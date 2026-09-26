import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402
from merge_ops import MergeRuleError  # noqa: E402

HASH_P = "a" * 64
HASH_S = "b" * 64
HASH_SHARED = "e" * 64


class ClaimMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, number, policy="P-1", loss=500000, urgent=True):
        return self.service.create_claim(
            "intake1", "intake", number, "TY-2026", "A区", "typhoon", policy, "R-" + number,
            30.1, 121.1, loss, urgent, False,
        )

    def live_pair(self):
        """同事件同保单但预估损失差异大，系统未自动标重的两件活案。"""
        primary = self.claim("C-P", loss=500000)
        primary = self.service.triage_claim("sup1", "supervisor", primary["id"], primary["version"], 0.1)
        primary = self.service.assign_claim("sup1", "supervisor", primary["id"], "adjuster1", primary["version"], "survey1")
        secondary = self.claim("C-S", loss=400000)
        secondary = self.service.triage_claim("sup1", "supervisor", secondary["id"], secondary["version"], 0.1)
        secondary = self.service.assign_claim("sup1", "supervisor", secondary["id"], "adjuster2", secondary["version"], "survey2")
        return primary, secondary

    def test_merge_releases_secondary_and_migrates_evidence(self):
        primary, secondary = self.live_pair()
        self.service.add_evidence("adjuster1", "adjuster", primary["id"], HASH_P, "p1.jpg", "field")
        self.service.add_evidence("adjuster1", "adjuster", primary["id"], HASH_SHARED, "same.jpg", "field")
        self.service.add_evidence("adjuster2", "adjuster", secondary["id"], HASH_S, "s1.jpg", "field")
        self.service.add_evidence("adjuster2", "adjuster", secondary["id"], HASH_SHARED, "same.jpg", "field")

        candidates = self.service.merge_candidates("sup1", "supervisor")["candidates"]
        pair = [c for c in candidates if c["secondary_id"] == secondary["id"]]
        self.assertEqual(1, len(pair))
        self.assertEqual(2, pair[0]["secondary_evidence"])
        self.assertFalse(pair[0]["over_cap"])

        result = self.service.merge_claims(
            "sup1", "supervisor", primary["id"], secondary["id"],
            primary["version"], secondary["version"],
        )
        self.assertEqual(1, result["evidence_moved"])
        self.assertEqual(1, result["evidence_kept"])  # 哈希与主案冲突的留在副案
        merged = result["secondary"]
        self.assertEqual("C-S(并入C-P)", merged["claim_no"])
        self.assertEqual("duplicate", merged["status"])
        self.assertEqual(primary["id"], merged["merged_into"])
        self.assertIsNone(merged["assignee"])  # 负责人与待办一起释放
        self.assertIsNone(merged["surveyor"])

        state = self.service.state("sup1", "supervisor")
        primary_evidence = [e for e in state["evidence"] if e["claim_id"] == primary["id"]]
        self.assertEqual(3, len(primary_evidence))
        migrated = [e for e in primary_evidence if e["sha256"] == HASH_S]
        self.assertEqual("C-S", migrated[0]["origin_claim_no"])  # 转挂证据保留原案号
        secondary_evidence = [e for e in state["evidence"] if e["claim_id"] == secondary["id"]]
        self.assertEqual([HASH_SHARED], [e["sha256"] for e in secondary_evidence])

    def test_secondary_advance_counts_toward_primary_cap(self):
        primary, secondary = self.live_pair()
        primary = self.service.emergency_advance("sup1", "supervisor", primary["id"], 50000, primary["version"], "ADV-P")
        secondary = self.service.emergency_advance("sup1", "supervisor", secondary["id"], 30000, secondary["version"], "ADV-S")
        result = self.service.merge_claims(
            "sup1", "supervisor", primary["id"], secondary["id"],
            primary["version"], secondary["version"],
        )
        self.assertEqual(80000, result["advance_total"])  # 副案预付计入主案
        self.assertEqual(100000, result["advance_cap"])  # 主案预估损失两成
        with self.assertRaises(DomainError) as ctx:
            self.service.emergency_advance("sup1", "supervisor", primary["id"], 20001, result["primary"]["version"], "ADV-X")
        self.assertEqual(409, ctx.exception.status)
        topped = self.service.emergency_advance("sup1", "supervisor", primary["id"], 20000, result["primary"]["version"], "ADV-T")
        self.assertEqual(70000, topped["emergency_advance"])  # 主案自身预付，不含副案

    def test_unmerge_restores_number_and_responsibility_keeps_evidence(self):
        primary, secondary = self.live_pair()
        self.service.add_evidence("adjuster2", "adjuster", secondary["id"], HASH_S, "s1.jpg", "field")
        result = self.service.merge_claims(
            "sup1", "supervisor", primary["id"], secondary["id"],
            primary["version"], secondary["version"],
        )
        restored = self.service.unmerge_claims(
            "sup1", "supervisor", secondary["id"], result["secondary"]["version"],
        )["secondary"]
        self.assertEqual("C-S", restored["claim_no"])  # 恢复原案号
        self.assertEqual("assigned", restored["status"])
        self.assertEqual("adjuster2", restored["assignee"])  # 恢复责任
        self.assertEqual("survey2", restored["surveyor"])
        self.assertIsNone(restored["merged_into"])
        state = self.service.state("sup1", "supervisor")
        primary_evidence = [e for e in state["evidence"] if e["claim_id"] == primary["id"]]
        self.assertEqual([HASH_S], [e["sha256"] for e in primary_evidence])  # 主案保留全部历史证据
        self.assertEqual("C-S", primary_evidence[0]["origin_claim_no"])
        self.assertEqual([], [m for m in state["merges"] if m["status"] == "active"])

    def test_system_detected_duplicate_is_mergeable(self):
        first = self.claim("C-A", policy="P-10")
        second = self.claim("C-B", policy="P-10")
        self.assertEqual("duplicate", second["status"])
        candidates = self.service.merge_candidates("sup1", "supervisor")["candidates"]
        self.assertEqual([(first["id"], second["id"])],
                         [(c["primary_id"], c["secondary_id"]) for c in candidates])
        result = self.service.merge_claims(
            "sup1", "supervisor", first["id"], second["id"], first["version"], second["version"],
        )
        self.assertEqual(first["id"], result["secondary"]["merged_into"])

    def test_merge_rule_violations(self):
        primary, secondary = self.live_pair()
        other = self.claim("C-X", policy="P-OTHER", loss=100000)
        with self.assertRaises(DomainError) as perm:  # 非主管不能并案
            self.service.merge_claims("adj1", "adjuster", primary["id"], secondary["id"],
                                      primary["version"], secondary["version"])
        self.assertEqual(403, perm.exception.status)
        with self.assertRaises(DomainError) as stale:  # 乐观锁
            self.service.merge_claims("sup1", "supervisor", primary["id"], secondary["id"], 99, 99)
        self.assertEqual(409, stale.exception.status)
        with self.assertRaises(MergeRuleError):  # 无重复关联
            self.service.merge_claims("sup1", "supervisor", primary["id"], other["id"],
                                      primary["version"], other["version"])
        result = self.service.merge_claims("sup1", "supervisor", primary["id"], secondary["id"],
                                           primary["version"], secondary["version"])
        with self.assertRaises(MergeRuleError):  # 重复并案
            self.service.merge_claims("sup1", "supervisor", primary["id"], secondary["id"],
                                      result["primary"]["version"], result["secondary"]["version"])
        with self.assertRaises(DomainError):  # 无并案记录不能改判
            self.service.unmerge_claims("sup1", "supervisor", other["id"], other["version"])

    def test_no_merge_or_unmerge_after_finalize(self):
        primary, secondary = self.live_pair()
        primary = self.service.record_survey("adjuster1", "adjuster", primary["id"], 0.5, "受损", "赔付", primary["version"])
        primary = self.service.submit_review("adjuster1", "adjuster", primary["id"], primary["version"])
        result = self.service.merge_claims(
            "sup1", "supervisor", primary["id"], secondary["id"], primary["version"], secondary["version"],
        )
        finalized = self.service.finalize_claim("sup1", "supervisor", primary["id"], "approve",
                                                300000, result["primary"]["version"])
        self.assertEqual("approved", finalized["status"])
        with self.assertRaises(DomainError):  # 主案核定后不能改判
            self.service.unmerge_claims("sup1", "supervisor", secondary["id"], result["secondary"]["version"])
        late = self.claim("C-L", policy="P-1", loss=400000)
        with self.assertRaises(MergeRuleError):  # 主案核定后不能并案
            self.service.merge_claims("sup1", "supervisor", primary["id"], late["id"],
                                      finalized["version"], late["version"])

    def test_over_cap_candidate_still_mergeable_but_blocks_new_advance(self):
        primary, secondary = self.live_pair()
        primary = self.service.emergency_advance("sup1", "supervisor", primary["id"], 50000, primary["version"], "ADV-P")
        secondary = self.service.emergency_advance("sup1", "supervisor", secondary["id"], 60000, secondary["version"], "ADV-S")
        pair = [c for c in self.service.merge_candidates("sup1", "supervisor")["candidates"]
                if c["secondary_id"] == secondary["id"]]
        self.assertTrue(pair[0]["over_cap"])  # 主管能预演看到并后超上限
        result = self.service.merge_claims("sup1", "supervisor", primary["id"], secondary["id"],
                                           primary["version"], secondary["version"])
        with self.assertRaises(DomainError):
            self.service.emergency_advance("sup1", "supervisor", primary["id"], 1, result["primary"]["version"], "ADV-X")

    def test_candidates_forbidden_for_adjuster(self):
        self.live_pair()
        with self.assertRaises(DomainError) as ctx:
            self.service.merge_candidates("adjuster1", "adjuster")
        self.assertEqual(403, ctx.exception.status)


if __name__ == "__main__":
    unittest.main()
