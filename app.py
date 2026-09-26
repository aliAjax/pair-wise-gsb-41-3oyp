"""Catastrophe insurance claim triage and settlement service."""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "catastrophe_claims.db"
TERMINAL = {"duplicate", "approved", "rejected", "closed", "merged"}
TRANSITIONS = {
    "received": {"triaged"},
    "triaged": {"assigned", "escalated"},
    "assigned": {"survey", "escalated"},
    "survey": {"review", "escalated"},
    "review": {"approved", "rejected", "escalated"},
    "escalated": {"assigned", "review", "rejected"},
}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def actor_id(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def coordinate(value: Any, label: str, low: float, high: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("%s必须是数值" % label) from exc
    if not low <= value <= high:
        raise DomainError("%s超出有效范围" % label)
    return value


class CatastropheClaimService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_no TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL,
                    region TEXT NOT NULL,
                    peril_type TEXT NOT NULL,
                    policy_no TEXT NOT NULL,
                    claimant_ref TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    estimated_loss REAL NOT NULL,
                    urgent_need INTEGER NOT NULL DEFAULT 0,
                    fraud_score REAL NOT NULL DEFAULT 0,
                    priority_score REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'received',
                    assignee TEXT,
                    surveyor TEXT,
                    lodging_required INTEGER NOT NULL DEFAULT 0,
                    remote_review INTEGER NOT NULL DEFAULT 0,
                    emergency_advance REAL NOT NULL DEFAULT 0,
                    final_payout REAL,
                    duplicate_of INTEGER REFERENCES claims(id),
                    merged_into INTEGER REFERENCES claims(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    sha256 TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    source TEXT NOT NULL,
                    submitter TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received',
                    origin_claim_no TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(claim_id,sha256)
                );
                CREATE TABLE IF NOT EXISTS survey_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    surveyor TEXT NOT NULL,
                    damage_ratio REAL NOT NULL,
                    findings TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    kind TEXT NOT NULL,
                    amount REAL NOT NULL,
                    approved_by TEXT NOT NULL,
                    reference TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER REFERENCES claims(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS merges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    primary_claim_id INTEGER NOT NULL REFERENCES claims(id),
                    secondary_claim_id INTEGER NOT NULL REFERENCES claims(id),
                    previous_status TEXT NOT NULL,
                    released_assignee TEXT,
                    released_surveyor TEXT,
                    carried_advance REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reverted_by TEXT,
                    reverted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_claims_queue ON claims(status, priority_score DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(sha256);
                CREATE INDEX IF NOT EXISTS idx_claims_merged_into ON claims(merged_into);
                """
            )
            for ddl in (
                "ALTER TABLE claims ADD COLUMN merged_into INTEGER REFERENCES claims(id)",
                "ALTER TABLE evidence ADD COLUMN origin_claim_no TEXT",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # 新库已含该列，旧库首次启动时补齐

    def _audit(self, conn: sqlite3.Connection, claim_id: int | None, actor: str, action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(claim_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (claim_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _claim(self, conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise DomainError("理赔案件不存在", 404)
        return row

    def create_claim(self, actor: str, role: str, claim_no: str, event_id: str,
                     region: str, peril_type: str, policy_no: str, claimant_ref: str,
                     latitude: float, longitude: float, estimated_loss: float,
                     urgent_need: bool = False, lodging_required: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"intake", "supervisor"}, "创建报案")
        values = [claim_no, event_id, region, peril_type, policy_no, claimant_ref]
        if not all(str(v).strip() for v in values):
            raise DomainError("案件必需字段不能为空")
        lat = coordinate(latitude, "纬度", -90, 90)
        lon = coordinate(longitude, "经度", -180, 180)
        try:
            estimated_loss = float(estimated_loss)
        except (TypeError, ValueError) as exc:
            raise DomainError("预估损失必须是数值") from exc
        if estimated_loss < 0:
            raise DomainError("预估损失不能为负数")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow()
            duplicate_of = None
            candidates = conn.execute(
                """SELECT * FROM claims WHERE event_id=? AND policy_no=? AND status NOT IN ('duplicate','merged')
                   ORDER BY id DESC LIMIT 50""",
                (event_id.strip(), policy_no.strip()),
            ).fetchall()
            for row in candidates:
                within_time = abs((datetime.fromisoformat(now) - datetime.fromisoformat(row["created_at"])).total_seconds()) <= 172800
                loss_close = abs(row["estimated_loss"] - estimated_loss) <= max(1000.0, row["estimated_loss"] * 0.1)
                if within_time and loss_close and haversine_km(lat, lon, row["latitude"], row["longitude"]) <= 3.0:
                    duplicate_of = row["id"]
                    break
            status = "duplicate" if duplicate_of else "received"
            try:
                cur = conn.execute(
                    """INSERT INTO claims(claim_no,event_id,region,peril_type,policy_no,claimant_ref,latitude,longitude,
                       estimated_loss,urgent_need,lodging_required,status,duplicate_of,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (claim_no.strip(), event_id.strip(), region.strip(), peril_type.strip(), policy_no.strip(),
                     claimant_ref.strip(), lat, lon, estimated_loss, int(bool(urgent_need)), int(bool(lodging_required)),
                     status, duplicate_of, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("报案编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "claim.created", {"duplicate_of": duplicate_of})
            if duplicate_of:
                self._audit(conn, duplicate_of, actor, "claim.duplicate_detected", {"new_claim": claim_no.strip()})
            return dict(self._claim(conn, cur.lastrowid))

    def triage_claim(self, actor: str, role: str, claim_id: int, expected_version: int,
                     fraud_score: float = 0.0, remote_review: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "案件分级")
        try:
            fraud_score = float(fraud_score)
        except (TypeError, ValueError) as exc:
            raise DomainError("欺诈评分必须是数值") from exc
        if not 0 <= fraud_score <= 1:
            raise DomainError("欺诈评分应在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "received":
                raise DomainError("只有待分级案件可以分级", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            priority = min(100.0, claim["estimated_loss"] / 100000.0 * 25 + (40 if claim["urgent_need"] else 0) + fraud_score * 20 + (10 if claim["lodging_required"] else 0))
            new_status = "escalated" if fraud_score >= 0.8 else "triaged"
            conn.execute(
                "UPDATE claims SET fraud_score=?,priority_score=?,remote_review=?,status=?,version=version+1,updated_at=? WHERE id=?",
                (fraud_score, priority, int(bool(remote_review)), new_status, utcnow(), claim_id),
            )
            self._audit(conn, claim_id, actor, "claim.triaged", {"priority": priority, "status": new_status})
            return dict(self._claim(conn, claim_id))

    def assign_claim(self, actor: str, role: str, claim_id: int, assignee: str,
                     expected_version: int, surveyor: str | None = None) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "分配案件")
        assignee = assignee.strip()
        if not assignee:
            raise DomainError("查勘负责人不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"triaged", "escalated", "assigned"}:
                raise DomainError("当前状态不能分配", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            conn.execute(
                """UPDATE claims SET assignee=?,surveyor=?,status='assigned',version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (assignee, surveyor.strip() if surveyor else None, utcnow(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.assigned", {"assignee": assignee, "surveyor": surveyor})
            return dict(self._claim(conn, claim_id))

    def add_evidence(self, actor: str, role: str, claim_id: int, sha256: str,
                     filename: str, source: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"intake", "adjuster", "surveyor", "supervisor"}, "添加损失证据")
        sha256 = sha256.strip().lower()
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise DomainError("证据哈希必须是 64 位 SHA-256 十六进制")
        if not filename.strip() or not source.strip():
            raise DomainError("证据文件名和来源不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] in TERMINAL:
                raise DomainError("已结束案件不能添加证据", 409)
            existing = conn.execute("SELECT * FROM evidence WHERE claim_id=? AND sha256=?", (claim_id, sha256)).fetchone()
            if existing:
                return dict(existing)
            cur = conn.execute(
                "INSERT INTO evidence(claim_id,sha256,filename,source,submitter,created_at) VALUES(?,?,?,?,?,?)",
                (claim_id, sha256, filename.strip(), source.strip(), actor, utcnow()),
            )
            hash_claims = [r["claim_id"] for r in conn.execute(
                "SELECT DISTINCT claim_id FROM evidence WHERE sha256=?", (sha256,)
            ).fetchall()]
            suspicious = len(hash_claims) >= 3
            if suspicious:
                for cid in hash_claims:
                    conn.execute(
                        "UPDATE claims SET fraud_score=MAX(fraud_score,0.95),status='escalated',version=version+1,updated_at=? WHERE id=? AND status<>'duplicate'",
                        (utcnow(), cid),
                    )
                self._audit(conn, claim_id, actor, "evidence.bulk_reuse_detected", {"sha256": sha256, "claim_ids": hash_claims})
            self._audit(conn, claim_id, actor, "evidence.added", {"evidence_id": cur.lastrowid, "suspicious": suspicious})
            return {"evidence": dict(conn.execute("SELECT * FROM evidence WHERE id=?", (cur.lastrowid,)).fetchone()), "bulk_reuse": suspicious, "affected_claims": hash_claims}

    def record_survey(self, actor: str, role: str, claim_id: int, damage_ratio: float,
                      findings: str, recommendation: str, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "录入查勘结果")
        try:
            damage_ratio = float(damage_ratio)
        except (TypeError, ValueError) as exc:
            raise DomainError("损失比例必须是数值") from exc
        if not 0 <= damage_ratio <= 1 or not findings.strip() or not recommendation.strip():
            raise DomainError("损失比例或查勘内容无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"assigned", "escalated"}:
                raise DomainError("当前状态不能录入查勘", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["assignee"] != actor and claim["surveyor"] != actor:
                raise DomainError("只有被分配的查勘人员可以录入结果", 403)
            if claim["status"] == "escalated" and claim["fraud_score"] >= 0.8:
                raise DomainError("高风险案件须先完成复核降险，不能直接提交查勘", 409)
            conn.execute(
                "INSERT INTO survey_notes(claim_id,surveyor,damage_ratio,findings,recommendation,created_at) VALUES(?,?,?,?,?,?)",
                (claim_id, actor, damage_ratio, findings.strip(), recommendation.strip(), utcnow()),
            )
            conn.execute("UPDATE claims SET status='survey',version=version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "survey.recorded", {"damage_ratio": damage_ratio, "recommendation": recommendation})
            return dict(self._claim(conn, claim_id))

    def submit_review(self, actor: str, role: str, claim_id: int, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "提交核损")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "survey":
                raise DomainError("只有已查勘案件可以提交核损", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if not conn.execute("SELECT 1 FROM survey_notes WHERE claim_id=?", (claim_id,)).fetchone():
                raise DomainError("缺少查勘记录", 409)
            conn.execute("UPDATE claims SET status='review',version=version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "claim.review_submitted", {})
            return dict(self._claim(conn, claim_id))

    def emergency_advance(self, actor: str, role: str, claim_id: int, amount: float,
                          expected_version: int, reference: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "批准紧急预付")
        try:
            amount = float(amount)
        except (TypeError, ValueError) as exc:
            raise DomainError("预付金额必须是数值") from exc
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if not claim["urgent_need"]:
                raise DomainError("非紧急案件不能预付", 409)
            if claim["status"] in TERMINAL:
                raise DomainError("当前案件状态不能预付", 409)
            if claim["fraud_score"] >= 0.8:
                raise DomainError("高风险案件不能预付", 409)
            limit = claim["estimated_loss"] * 0.2
            if amount <= 0 or amount > limit:
                raise DomainError("预付金额必须大于0且不超过预估损失的20%", 409)
            if self._advance_usage(conn, claim) + amount > limit:
                raise DomainError("累计预付（含并案副案预付）超过上限", 409)
            try:
                conn.execute(
                    "INSERT INTO payments(claim_id,kind,amount,approved_by,reference,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, "emergency_advance", amount, actor, reference.strip(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("付款参考号已存在", 409) from exc
            conn.execute("UPDATE claims SET emergency_advance=emergency_advance+?,version=version+1,updated_at=? WHERE id=?", (amount, utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "payment.emergency_advance", {"amount": amount, "reference": reference})
            return dict(self._claim(conn, claim_id))

    def finalize_claim(self, actor: str, role: str, claim_id: int, decision: str,
                       payout: float, expected_version: int, reason: str = "") -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "最终核定")
        if decision not in {"approve", "reject"}:
            raise DomainError("核定决定无效")
        try:
            payout = float(payout)
        except (TypeError, ValueError) as exc:
            raise DomainError("核定金额必须是数值") from exc
        if payout < 0:
            raise DomainError("核定金额不能为负数")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "review":
                raise DomainError("只有待复核案件可以最终核定", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["duplicate_of"]:
                raise DomainError("重复报案不能核定赔付", 409)
            if claim["fraud_score"] >= 0.8 and decision == "approve":
                raise DomainError("高风险案件未解除风险标记，不能赔付", 409)
            if decision == "approve" and payout > claim["estimated_loss"]:
                raise DomainError("核定金额不能超过预估损失", 409)
            if decision == "reject" and not reason.strip():
                raise DomainError("拒赔必须填写理由", 409)
            status = "approved" if decision == "approve" else "rejected"
            conn.execute(
                "UPDATE claims SET status=?,final_payout=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (status, payout if decision == "approve" else 0, utcnow(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.finalized", {"decision": decision, "payout": payout, "reason": reason.strip()})
            return dict(self._claim(conn, claim_id))

    # ---- 归并规则 ----
    def _merge_pair(self, conn: sqlite3.Connection, primary_id: int, secondary_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
        if primary_id == secondary_id:
            raise DomainError("主案和副案不能是同一案件")
        primary = self._claim(conn, primary_id)
        secondary = self._claim(conn, secondary_id)
        if primary["status"] in TERMINAL:
            raise DomainError("主案已结案或被并案，不能作为并案主案", 409)
        if secondary["status"] in {"approved", "rejected", "closed", "merged"}:
            raise DomainError("副案当前状态不能并案", 409)
        if primary["event_id"] != secondary["event_id"] or primary["policy_no"] != secondary["policy_no"]:
            raise DomainError("只有同一事件同一保单的案件可以并案", 409)
        chained = conn.execute(
            "SELECT 1 FROM merges WHERE primary_claim_id=? AND status='active'", (secondary_id,)
        ).fetchone()
        if chained:
            raise DomainError("副案本身是其他并案的主案，不能级联并案", 409)
        return primary, secondary

    # ---- 证据迁移 ----
    def _migrate_evidence(self, conn: sqlite3.Connection, primary: sqlite3.Row, secondary: sqlite3.Row) -> tuple[int, list[dict[str, Any]]]:
        moved = conn.execute(
            """UPDATE evidence SET claim_id=?,origin_claim_no=?
               WHERE claim_id=? AND sha256 NOT IN (SELECT sha256 FROM evidence WHERE claim_id=?)""",
            (primary["id"], secondary["claim_no"], secondary["id"], primary["id"]),
        ).rowcount
        dropped = [dict(r) for r in conn.execute(
            "SELECT sha256,filename,source,submitter FROM evidence WHERE claim_id=?", (secondary["id"],)
        ).fetchall()]
        if dropped:
            conn.execute("DELETE FROM evidence WHERE claim_id=?", (secondary["id"],))
        return moved, dropped

    # ---- 付款校验 ----
    def _advance_usage(self, conn: sqlite3.Connection, claim: sqlite3.Row) -> float:
        usage = claim["emergency_advance"]
        rows = conn.execute(
            "SELECT emergency_advance FROM claims WHERE merged_into=? AND status='merged'", (claim["id"],)
        ).fetchall()
        return usage + sum(row["emergency_advance"] for row in rows)

    def merge_claims(self, actor: str, role: str, primary_claim_id: int, secondary_claim_id: int,
                     expected_version: int, secondary_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "并案")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            primary, secondary = self._merge_pair(conn, int(primary_claim_id), int(secondary_claim_id))
            if primary["version"] != int(expected_version) or secondary["version"] != int(secondary_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            moved, dropped = self._migrate_evidence(conn, primary, secondary)
            now = utcnow()
            cur = conn.execute(
                """INSERT INTO merges(primary_claim_id,secondary_claim_id,previous_status,released_assignee,
                   released_surveyor,carried_advance,status,actor,created_at) VALUES(?,?,?,?,?,?,'active',?,?)""",
                (primary["id"], secondary["id"], secondary["status"], secondary["assignee"],
                 secondary["surveyor"], secondary["emergency_advance"], actor, now),
            )
            conn.execute(
                """UPDATE claims SET status='merged',merged_into=?,assignee=NULL,surveyor=NULL,
                   version=version+1,updated_at=? WHERE id=?""",
                (primary["id"], now, secondary["id"]),
            )
            conn.execute("UPDATE claims SET version=version+1,updated_at=? WHERE id=?", (now, primary["id"]))
            self._audit(conn, primary["id"], actor, "claim.merged", {
                "secondary_claim_no": secondary["claim_no"],
                "evidence_moved": moved,
                "evidence_duplicates_dropped": dropped,
                "released_assignee": secondary["assignee"],
                "released_surveyor": secondary["surveyor"],
                "carried_advance": secondary["emergency_advance"],
            })
            self._audit(conn, secondary["id"], actor, "claim.merged_into", {"primary_claim_no": primary["claim_no"]})
            updated_primary = self._claim(conn, primary["id"])
            return {
                "merge": dict(conn.execute("SELECT * FROM merges WHERE id=?", (cur.lastrowid,)).fetchone()),
                "primary": dict(updated_primary),
                "secondary": dict(self._claim(conn, secondary["id"])),
                "evidence_moved": moved,
                "evidence_duplicates_dropped": len(dropped),
                "advance_usage": self._advance_usage(conn, updated_primary),
                "advance_limit": updated_primary["estimated_loss"] * 0.2,
            }

    def unmerge_claim(self, actor: str, role: str, secondary_claim_id: int, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "并案改判")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            secondary = self._claim(conn, int(secondary_claim_id))
            merge = conn.execute(
                "SELECT * FROM merges WHERE secondary_claim_id=? AND status='active' ORDER BY id DESC", (secondary["id"],)
            ).fetchone()
            if not merge or secondary["status"] != "merged":
                raise DomainError("该案件没有生效中的并案", 409)
            primary = self._claim(conn, merge["primary_claim_id"])
            if primary["status"] in {"approved", "rejected", "closed"}:
                raise DomainError("主案已核定结案，不能改判拆分", 409)
            if secondary["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            now = utcnow()
            conn.execute(
                """UPDATE claims SET status=?,assignee=?,surveyor=?,merged_into=NULL,version=version+1,updated_at=?
                   WHERE id=?""",
                (merge["previous_status"], merge["released_assignee"], merge["released_surveyor"], now, secondary["id"]),
            )
            conn.execute("UPDATE claims SET version=version+1,updated_at=? WHERE id=?", (now, primary["id"]))
            conn.execute("UPDATE merges SET status='reverted',reverted_by=?,reverted_at=? WHERE id=?", (actor, now, merge["id"]))
            kept = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence WHERE claim_id=? AND origin_claim_no=?",
                (primary["id"], secondary["claim_no"]),
            ).fetchone()["c"]
            self._audit(conn, primary["id"], actor, "claim.unmerged", {"secondary_claim_no": secondary["claim_no"], "evidence_retained": kept})
            self._audit(conn, secondary["id"], actor, "claim.unmerge_restored", {
                "primary_claim_no": primary["claim_no"],
                "restore_status": merge["previous_status"],
                "assignee": merge["released_assignee"],
                "surveyor": merge["released_surveyor"],
            })
            return {
                "merge": dict(conn.execute("SELECT * FROM merges WHERE id=?", (merge["id"],)).fetchone()),
                "primary": dict(self._claim(conn, primary["id"])),
                "secondary": dict(self._claim(conn, secondary["id"])),
                "evidence_retained": kept,
            }

    def merge_candidates(self, role: str) -> list[dict[str, Any]]:
        require_role(role, {"supervisor", "auditor"}, "查看并案候选")
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT p.id AS primary_claim_id,p.claim_no AS primary_claim_no,p.status AS primary_status,
                          p.version AS primary_version,p.emergency_advance AS primary_advance,
                          s.id AS secondary_claim_id,s.claim_no AS secondary_claim_no,s.status AS secondary_status,
                          s.version AS secondary_version,s.emergency_advance AS secondary_advance
                   FROM claims p JOIN claims s
                     ON p.event_id=s.event_id AND p.policy_no=s.policy_no AND p.id<>s.id
                   WHERE p.status NOT IN ('duplicate','approved','rejected','closed','merged')
                     AND s.status NOT IN ('approved','rejected','closed','merged')
                   ORDER BY p.id,s.id"""
            ).fetchall()
            chained = {r["primary_claim_id"] for r in conn.execute(
                "SELECT primary_claim_id FROM merges WHERE status='active'"
            ).fetchall()}
        return [dict(r) for r in rows if r["secondary_claim_id"] not in chained]

    def queue(self, role: str = "viewer", actor: str = "") -> list[dict[str, Any]]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}:
            raise DomainError("角色无权查看理赔队列", 403)
        with self.connect() as conn:
            if role in {"adjuster", "surveyor"}:
                rows = conn.execute(
                    "SELECT * FROM claims WHERE (assignee=? OR surveyor=?) AND status NOT IN ('approved','rejected','duplicate','merged') ORDER BY priority_score DESC,created_at",
                    (actor, actor),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,created_at").fetchall()
        return [dict(r) for r in rows]

    def state(self, actor: str = "", role: str = "viewer") -> dict[str, Any]:
        allowed = role in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}
        if not allowed:
            return {"claims": [], "evidence": [], "payments": [], "timeline": [], "merges": [], "access_limited": True}
        with self.connect() as conn:
            if role in {"adjuster", "surveyor"}:
                claims = [dict(r) for r in conn.execute(
                    "SELECT * FROM claims WHERE assignee=? OR surveyor=? ORDER BY priority_score DESC,id DESC", (actor, actor)
                ).fetchall()]
            else:
                claims = [dict(r) for r in conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,id DESC").fetchall()]
            ids = [c["id"] for c in claims]
            if ids:
                marks = ",".join("?" for _ in ids)
                evidence = [dict(r) for r in conn.execute("SELECT * FROM evidence WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall()]
                payments = [dict(r) for r in conn.execute("SELECT * FROM payments WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall()]
                timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline WHERE claim_id IN (%s) ORDER BY id DESC LIMIT 300" % marks, ids).fetchall()]
                merges = [dict(r) for r in conn.execute(
                    "SELECT * FROM merges WHERE primary_claim_id IN (%s) OR secondary_claim_id IN (%s) ORDER BY id DESC" % (marks, marks),
                    ids + ids,
                ).fetchall()]
            else:
                evidence, payments, timeline, merges = [], [], [], []
        usage = {c["id"]: c["emergency_advance"] for c in claims}
        for c in claims:
            if c["status"] == "merged" and c["merged_into"] in usage:
                usage[c["merged_into"]] += c["emergency_advance"]
        for c in claims:
            c["advance_usage"] = round(usage[c["id"]], 2)
            c["advance_limit"] = round(c["estimated_loss"] * 0.2, 2)
        return {"claims": claims, "evidence": evidence, "payments": payments, "timeline": timeline, "merges": merges, "access_limited": False}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM claims").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        c1 = self.create_claim("intake-demo", "intake", "CLM-DEMO-001", "TY2026", "沿海A区", "洪水", "P-1001", "R-01", 30.1, 121.2, 500000, True, True)
        self.create_claim("intake-demo", "intake", "CLM-DEMO-002", "TY2026", "沿海A区", "洪水", "P-1002", "R-02", 30.2, 121.3, 240000, False, False)
        return {"seeded": True, "first_claim_id": c1["id"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: CatastropheClaimService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "viewer")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/health":
                self._send(200, {"status": "ok", "service": "catastrophe-claims"})
            elif path == "/api/state":
                self._send(200, self.service.state(*self._headers()))
            elif path == "/api/queue":
                actor, role = self._headers()
                self._send(200, {"queue": self.service.queue(role, actor)})
            elif path == "/api/claims/merge-candidates":
                actor, role = self._headers()
                self._send(200, {"candidates": self.service.merge_candidates(role)})
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/claims":
                result = self.service.create_claim(actor, role, **data)
            elif path == "/api/claims/triage":
                result = self.service.triage_claim(actor, role, **data)
            elif path == "/api/claims/assign":
                result = self.service.assign_claim(actor, role, **data)
            elif path == "/api/evidence":
                result = self.service.add_evidence(actor, role, **data)
            elif path == "/api/claims/survey":
                result = self.service.record_survey(actor, role, **data)
            elif path == "/api/claims/submit-review":
                result = self.service.submit_review(actor, role, **data)
            elif path == "/api/claims/emergency-advance":
                result = self.service.emergency_advance(actor, role, **data)
            elif path == "/api/claims/finalize":
                result = self.service.finalize_claim(actor, role, **data)
            elif path == "/api/claims/merge":
                result = self.service.merge_claims(actor, role, **data)
            elif path == "/api/claims/unmerge":
                result = self.service.unmerge_claim(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: CatastropheClaimService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Catastrophe claim service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="巨灾保险理赔调度服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8207)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = CatastropheClaimService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
