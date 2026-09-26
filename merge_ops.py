"""并案归并规则、证据迁移与付款校验。

与接口层（app.py）和页面（static/index.html）分开实现：
- find_merge_candidates / assert_mergeable : 归并规则（主管确认前的校验与候选预演）
- migrate_evidence                         : 证据迁移（副案证据转挂主案并保留原案号）
- group_emergency_advance                  : 付款校验（副案已有预付计入主案两成上限）

只依赖标准库 sqlite3，函数接收已开启事务的连接，由调用方提交。
"""
from __future__ import annotations

import sqlite3
from typing import Any

# 已核定结案的状态：主案结案后不能再并案或改判
FINALIZED = {"approved", "rejected", "closed"}
# 紧急预付上限：预估损失的两成
ADVANCE_CAP_RATIO = 0.2


class MergeRuleError(Exception):
    """归并规则校验失败。"""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def active_merge(conn: sqlite3.Connection, secondary_id: int) -> sqlite3.Row | None:
    """副案当前进行中的并案记录（没有则为 None）。"""
    return conn.execute(
        "SELECT * FROM claim_merges WHERE secondary_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (secondary_id,),
    ).fetchone()


def assert_mergeable(primary: sqlite3.Row, secondary: sqlite3.Row,
                     existing_merge: sqlite3.Row | None) -> None:
    """归并规则：主管确认并案前必须全部通过。"""
    if primary["id"] == secondary["id"]:
        raise MergeRuleError("主案与副案不能是同一件")
    if primary["status"] in FINALIZED:
        raise MergeRuleError("主案已核定结案，不能并案")
    if primary["status"] == "duplicate":
        raise MergeRuleError("主案本身是重复报案，应先并入其主案")
    if primary["merged_into"]:
        raise MergeRuleError("主案本身已并入他案，不能级联并案")
    if secondary["status"] in FINALIZED:
        raise MergeRuleError("副案已核定结案，不能并案")
    if secondary["merged_into"] or existing_merge:
        raise MergeRuleError("副案已并入其他主案")
    linked = secondary["duplicate_of"] == primary["id"]
    same_loss = (secondary["event_id"] == primary["event_id"]
                 and secondary["policy_no"] == primary["policy_no"])
    if not (linked or same_loss):
        raise MergeRuleError("两案缺少重复报案关联（事件与保单不一致），不能并案")


def _candidate_pairs(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """候选对来源：系统识别的重复报案链 + 同事件同保单但漏标的先后两案。"""
    pairs: dict[tuple[int, int], None] = {}
    for row in conn.execute(
        "SELECT id, duplicate_of FROM claims WHERE duplicate_of IS NOT NULL AND merged_into IS NULL"
    ).fetchall():
        pairs[(row["duplicate_of"], row["id"])] = None
    for row in conn.execute(
        """SELECT a.id AS primary_id, b.id AS secondary_id FROM claims a
           JOIN claims b ON b.event_id=a.event_id AND b.policy_no=a.policy_no AND b.id>a.id
           WHERE a.merged_into IS NULL AND b.merged_into IS NULL"""
    ).fetchall():
        pairs[(row["primary_id"], row["secondary_id"])] = None
    return sorted(pairs)


def find_merge_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """归并规则：列出主管当前可并的候选对，并预演证据迁移与预付两成上限。"""
    candidates: list[dict[str, Any]] = []
    for primary_id, secondary_id in _candidate_pairs(conn):
        primary = conn.execute("SELECT * FROM claims WHERE id=?", (primary_id,)).fetchone()
        secondary = conn.execute("SELECT * FROM claims WHERE id=?", (secondary_id,)).fetchone()
        if not primary or not secondary:
            continue
        try:
            assert_mergeable(primary, secondary, active_merge(conn, secondary_id))
        except MergeRuleError:
            continue
        evidence_count = conn.execute(
            "SELECT COUNT(*) AS c FROM evidence WHERE claim_id=?", (secondary_id,)
        ).fetchone()["c"]
        cap = primary["estimated_loss"] * ADVANCE_CAP_RATIO
        advance_after = group_emergency_advance(conn, primary_id) + secondary["emergency_advance"]
        candidates.append({
            "primary_id": primary_id,
            "primary_claim_no": primary["claim_no"],
            "primary_status": primary["status"],
            "primary_version": primary["version"],
            "secondary_id": secondary_id,
            "secondary_claim_no": secondary["claim_no"],
            "secondary_status": secondary["status"],
            "secondary_version": secondary["version"],
            "secondary_evidence": evidence_count,
            "secondary_advance": secondary["emergency_advance"],
            "advance_after_merge": advance_after,
            "advance_cap": cap,
            "over_cap": advance_after > cap,
        })
    return candidates


def migrate_evidence(conn: sqlite3.Connection, primary_id: int, secondary_id: int,
                     origin_claim_no: str) -> tuple[list[int], list[int]]:
    """证据迁移：副案证据转挂主案，origin_claim_no 保留副案原案号。

    返回 (moved, kept)：哈希与主案已有证据冲突的留在副案（同一文件主案已留存，
    主案侧不重复挂接）。迁移到主案的证据在改判后仍保留在主案。
    """
    moved: list[int] = []
    kept: list[int] = []
    rows = conn.execute(
        "SELECT id, sha256 FROM evidence WHERE claim_id=? ORDER BY id", (secondary_id,)
    ).fetchall()
    for row in rows:
        conflict = conn.execute(
            "SELECT 1 FROM evidence WHERE claim_id=? AND sha256=?", (primary_id, row["sha256"])
        ).fetchone()
        if conflict:
            kept.append(row["id"])
            continue
        conn.execute(
            "UPDATE evidence SET claim_id=?, origin_claim_no=? WHERE id=?",
            (primary_id, origin_claim_no, row["id"]),
        )
        moved.append(row["id"])
    return moved, kept


def group_emergency_advance(conn: sqlite3.Connection, claim_id: int) -> float:
    """付款校验：主案有效预付 = 自身预付 + 各已并入副案的预付。

    主案核定前，副案已有预付计入主案预估损失两成的上限；改判解除并案后
    副案预付自然不再计入（merged_into 已清空）。
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(emergency_advance),0) AS total FROM claims WHERE id=? OR merged_into=?",
        (claim_id, claim_id),
    ).fetchone()
    return float(row["total"])
