import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402


class CatastropheClaimFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, number, policy="P-1", lat=30.1, loss=500000, urgent=False):
        return self.service.create_claim(
            "intake1", "intake", number, "TY-2026", "A区", "flood", policy, "R-" + number,
            lat, 121.1, loss, urgent, True,
        )

    def test_complete_claim_lifecycle_with_emergency_advance(self):
        claim = self.claim("C-001", urgent=True)
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1, True)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"], "survey1")
        claim = self.service.emergency_advance("sup1", "supervisor", claim["id"], 50000, claim["version"], "ADV-001")
        evidence = self.service.add_evidence("adjuster1", "adjuster", claim["id"], "a" * 64, "loss.jpg", "field")
        self.assertFalse(evidence["bulk_reuse"])
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.6, "结构受损", "部分赔付", claim["version"])
        claim = self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        claim = self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", 280000, claim["version"])
        self.assertEqual("approved", claim["status"])
        self.assertEqual(280000, claim["final_payout"])
        self.assertEqual(1, len(self.service.state("sup1", "supervisor")["payments"]))

    def test_duplicate_and_version_conflict(self):
        first = self.claim("C-010", policy="P-10")
        second = self.claim("C-011", policy="P-10", lat=30.11)
        self.assertEqual("duplicate", second["status"])
        self.assertEqual(first["id"], second["duplicate_of"])
        triaged = self.service.triage_claim("sup1", "supervisor", first["id"], first["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_claim("sup1", "supervisor", first["id"], "adjuster1", first["version"])
        self.assertEqual(409, ctx.exception.status)
        assigned = self.service.assign_claim("sup1", "supervisor", first["id"], "adjuster1", triaged["version"])
        self.assertEqual("assigned", assigned["status"])

    def test_bulk_forged_evidence_and_permissions(self):
        claims = [self.claim("C-%03d" % i, policy="P-%03d" % i, lat=30 + i / 100) for i in range(1, 4)]
        shared = "b" * 64
        last = None
        for claim in claims:
            last = self.service.add_evidence("intake1", "intake", claim["id"], shared, "same.pdf", "batch-import")
        self.assertTrue(last["bulk_reuse"])
        self.assertGreaterEqual(len(last["affected_claims"]), 3)
        with self.assertRaises(DomainError) as ctx:
            self.service.queue("viewer", "viewer")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.add_evidence("adjuster1", "adjuster", claims[0]["id"], "not-a-hash", "x", "field")
        self.assertEqual(400, ctx2.exception.status)


class ClaimMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def pair(self):
        primary = self.service.create_claim(
            "intake1", "intake", "M-001", "TY-2026", "A区", "flood", "P-M1", "R-1", 30.1, 121.1, 500000, True, False)
        secondary = self.service.create_claim(
            "intake2", "intake", "M-002", "TY-2026", "A区", "flood", "P-M1", "R-1", 30.1, 121.1, 600000, True, False)
        self.assertEqual("received", secondary["status"])
        return primary, secondary

    def advance_secondary(self, secondary):
        secondary = self.service.triage_claim("sup1", "supervisor", secondary["id"], secondary["version"], 0.1)
        secondary = self.service.assign_claim("sup1", "supervisor", secondary["id"], "adjuster2", secondary["version"], "survey2")
        secondary = self.service.emergency_advance("sup1", "supervisor", secondary["id"], 80000, secondary["version"], "ADV-M1")
        self.service.add_evidence("adjuster2", "adjuster", secondary["id"], "c" * 64, "roof.jpg", "field")
        return secondary

    def test_merge_moves_evidence_releases_workload_and_counts_secondary_advance(self):
        primary, secondary = self.pair()
        secondary = self.advance_secondary(secondary)
        self.service.add_evidence("intake1", "intake", primary["id"], "d" * 64, "wall.jpg", "hotline")

        result = self.service.merge_claims("sup1", "supervisor", primary["id"], secondary["id"], primary["version"], secondary["version"])
        self.assertEqual("merged", result["secondary"]["status"])
        self.assertEqual(primary["id"], result["secondary"]["merged_into"])
        self.assertIsNone(result["secondary"]["assignee"])
        self.assertIsNone(result["secondary"]["surveyor"])
        self.assertEqual(1, result["evidence_moved"])
        self.assertEqual(80000, result["advance_usage"])
        self.assertEqual(100000, result["advance_limit"])

        state = self.service.state("sup1", "supervisor")
        moved = [e for e in state["evidence"] if e["claim_id"] == primary["id"]]
        self.assertEqual(2, len(moved))
        origin = {e["filename"]: e["origin_claim_no"] for e in moved}
        self.assertEqual("M-002", origin["roof.jpg"])
        self.assertIsNone(origin["wall.jpg"])
        self.assertEqual([], self.service.queue("adjuster", "adjuster2"))

        with self.assertRaises(DomainError) as ctx:
            self.service.emergency_advance("sup1", "supervisor", primary["id"], 30000, result["primary"]["version"], "ADV-M2")
        self.assertEqual(409, ctx.exception.status)
        updated = self.service.emergency_advance("sup1", "supervisor", primary["id"], 20000, result["primary"]["version"], "ADV-M2")
        self.assertEqual(20000, updated["emergency_advance"])

        with self.assertRaises(DomainError):
            self.service.emergency_advance("sup1", "supervisor", secondary["id"], 1000, result["secondary"]["version"], "ADV-M3")
        with self.assertRaises(DomainError):
            self.service.add_evidence("adjuster2", "adjuster", secondary["id"], "e" * 64, "x.jpg", "field")

    def test_unmerge_restores_secondary_and_primary_keeps_evidence(self):
        primary, secondary = self.pair()
        secondary = self.advance_secondary(secondary)
        merged = self.service.merge_claims("sup1", "supervisor", primary["id"], secondary["id"], primary["version"], secondary["version"])

        restored = self.service.unmerge_claim("sup1", "supervisor", secondary["id"], merged["secondary"]["version"])
        self.assertEqual("assigned", restored["secondary"]["status"])
        self.assertEqual("adjuster2", restored["secondary"]["assignee"])
        self.assertEqual("survey2", restored["secondary"]["surveyor"])
        self.assertIsNone(restored["secondary"]["merged_into"])
        self.assertEqual("reverted", restored["merge"]["status"])
        self.assertEqual(1, restored["evidence_retained"])

        state = self.service.state("sup1", "supervisor")
        kept = [e for e in state["evidence"] if e["claim_id"] == primary["id"] and e["origin_claim_no"] == "M-002"]
        self.assertEqual(1, len(kept))
        self.assertEqual(1, len(self.service.queue("adjuster", "adjuster2")))

        updated = self.service.emergency_advance("sup1", "supervisor", primary["id"], 90000, restored["primary"]["version"], "ADV-M4")
        self.assertEqual(90000, updated["emergency_advance"])

    def test_merge_rules_and_candidates(self):
        primary, secondary = self.pair()
        other = self.service.create_claim(
            "intake1", "intake", "M-003", "TY-2026", "A区", "flood", "P-OTHER", "R-3", 30.5, 121.5, 100000, False, False)

        candidates = self.service.merge_candidates("supervisor")
        pairs = {(c["primary_claim_id"], c["secondary_claim_id"]) for c in candidates}
        self.assertIn((primary["id"], secondary["id"]), pairs)
        with self.assertRaises(DomainError) as ctx:
            self.service.merge_candidates("adjuster")
        self.assertEqual(403, ctx.exception.status)

        with self.assertRaises(DomainError) as ctx2:
            self.service.merge_claims("sup1", "supervisor", primary["id"], other["id"], primary["version"], other["version"])
        self.assertEqual(409, ctx2.exception.status)
        with self.assertRaises(DomainError) as ctx3:
            self.service.merge_claims("adjuster1", "adjuster", primary["id"], secondary["id"], primary["version"], secondary["version"])
        self.assertEqual(403, ctx3.exception.status)
        with self.assertRaises(DomainError):
            self.service.merge_claims("sup1", "supervisor", primary["id"], primary["id"], primary["version"], primary["version"])
        with self.assertRaises(DomainError):
            self.service.unmerge_claim("sup1", "supervisor", secondary["id"], secondary["version"])

        dup = self.service.create_claim(
            "intake1", "intake", "M-004", "TY-2026", "A区", "flood", "P-M1", "R-1", 30.1, 121.1, 510000, False, False)
        self.assertEqual("duplicate", dup["status"])
        done = self.service.merge_claims("sup1", "supervisor", primary["id"], dup["id"], primary["version"], dup["version"])
        self.assertEqual("merged", done["secondary"]["status"])
        with self.assertRaises(DomainError) as ctx4:
            self.service.merge_claims("sup1", "supervisor", primary["id"], dup["id"], done["primary"]["version"], done["secondary"]["version"])
        self.assertEqual(409, ctx4.exception.status)

    def test_merge_with_duplicate_evidence_hash(self):
        primary, secondary = self.pair()
        self.service.add_evidence("intake1", "intake", primary["id"], "f" * 64, "site.jpg", "hotline")
        self.service.add_evidence("intake2", "intake", secondary["id"], "f" * 64, "site-copy.jpg", "survey")
        self.service.add_evidence("intake2", "intake", secondary["id"], "0" * 64, "extra.jpg", "survey")

        result = self.service.merge_claims("sup1", "supervisor", primary["id"], secondary["id"], primary["version"], secondary["version"])
        self.assertEqual(1, result["evidence_moved"])
        self.assertEqual(1, result["evidence_duplicates_dropped"])
        state = self.service.state("sup1", "supervisor")
        hashes = [e["sha256"] for e in state["evidence"] if e["claim_id"] == primary["id"]]
        self.assertEqual(sorted(["f" * 64, "0" * 64]), sorted(hashes))


if __name__ == "__main__":
    unittest.main()
