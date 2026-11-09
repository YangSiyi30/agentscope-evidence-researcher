import copy
import unittest

from as_researcher.comparison import check_comparison, prepare_comparison, render_comparison
from as_researcher.revision import answer_chars, digest


def example():
    run = {
        "answer": "😀仅版本 v2 的 stdio 不得写 stdout 日志。[SRC-12345678@page]\n不能推出禁止日志文件。[SRC-12345678@page]",
        "sources": [{"id": "SRC-12345678", "content": "Only a discovery snippet."}],
        "page_evidence": [
            {
                "id": "SRC-12345678",
                "content": "v2 stdio: no stdout logs; log files are not prohibited.",
            }
        ],
    }
    candidate = "v2 stdio 不得写 stdout 日志；不禁止日志文件。[SRC-12345678@page]"
    revision = {
        "status": "applied_checks_only",
        "attempts": 1,
        "applied": True,
        "errors": [],
        "semantic_review": "required_not_automated",
        "original_answer": run["answer"],
        "original_sha256": digest(run["answer"]),
        "original_chars": answer_chars(run["answer"]),
        "candidate_answer": candidate,
        "candidate_sha256": digest(candidate),
        "candidate_chars": answer_chars(candidate),
    }
    return run, revision


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.run, self.revision = example()
        self.packet = prepare_comparison(self.run, self.revision)

    def check(self):
        return check_comparison(self.run, self.revision, self.packet)

    def test_separate_offsets_no_automatic_atomicity_mapping_or_human_score(self):
        result = self.check()
        self.assertTrue(result["valid"])
        self.assertFalse(result["review_complete"])
        self.assertIsNone(result["fidelity_score"])
        self.assertEqual(result["pending_mappings"], 2)
        self.assertEqual(result["unresolved_claims"]["candidate"], ["C001.A01"])
        self.assertEqual(self.packet["reviewer"]["kind"], "unassigned")
        for side in ("original", "candidate"):
            p = self.packet[side]
            for unit in p["units"]:
                self.assertEqual(p["answer"][unit["start"] : unit["end"]], unit["text"])
            self.assertFalse(p["atomicity_checked"])
        self.assertNotEqual(
            self.packet["original"]["run_sha256"], self.packet["candidate"]["run_sha256"]
        )

    def test_changed_source_or_revision_never_reuses_old_claim_positions(self):
        self.run["sources"][0]["content"] += " changed"
        self.assertIn("comparison_fingerprint_mismatch", self.check()["errors"])
        self.setUp()
        self.revision["candidate_answer"] = "Changed"
        self.assertFalse(self.check()["valid"])
        with self.assertRaises(ValueError):
            render_comparison(self.run, self.revision, self.packet)

    def test_many_original_claims_can_map_to_one_candidate_without_quality_claim(self):
        self.packet["reviewer"] = {"kind": "assistant", "name": "fixture reviewer"}
        self.packet["mappings"] = [
            {
                "mapping_id": "M1",
                "original": ["C001.A01", "C002.A01"],
                "candidate": ["C001.A01"],
                "relation": "preserved",
                "reason": "Two original sentences merged; this is a reviewer assertion.",
            }
        ]
        self.packet["mapping_coverage_checked"] = True
        result = self.check()
        self.assertTrue(result["valid"])
        self.assertEqual(result["unresolved_claims"], {"original": [], "candidate": []})
        self.assertFalse(result["review_complete"])  # Atomic packets still pending.
        self.assertIsNone(result["fidelity_score"])

    def test_omitted_added_and_unknown_links_are_not_interchangeable(self):
        for changes in (
            {"relation": "omitted", "candidate": ["C001.A01"]},
            {"relation": "added"},
            {"relation": "changed"},
            {"candidate": ["C999.A01"]},
            {"original": ["C001.A01", "C001.A01"]},
        ):
            with self.subTest(changes=changes):
                self.setUp()
                self.packet["mappings"][0].update(changes)
                self.assertFalse(self.check()["valid"])

    def test_pending_or_removed_claims_cannot_be_declared_fully_mapped(self):
        self.packet["mapping_coverage_checked"] = True
        self.assertIn("mapping_coverage_claim_incomplete", self.check()["errors"])
        self.setUp()
        self.packet["candidate"]["units"] = []
        self.assertFalse(self.check()["valid"])

    def test_evidence_quote_must_match_the_selected_level_on_each_side(self):
        draft = self.packet["candidate"]
        draft["reviewer"] = {"kind": "assistant", "name": "fixture reviewer"}
        claim = draft["units"][0]["claims"][0]
        claim.update(
            label="supported",
            reason="intentional mismatch",
            evidence=[
                {
                    "source_id": "SRC-12345678",
                    "kind": "snippet",
                    "quote": "Only a discovery snippet.",
                }
            ],
        )
        self.assertTrue(any("support_evidence_level_mismatch" in e for e in self.check()["errors"]))

    def test_html_is_literal_and_rendering_never_changes_review_labels(self):
        hostile = '<script>alert(1)</script><img src="https://bad.invalid/" onerror="x()">'
        self.run["sources"][0]["content"] = hostile
        self.packet = prepare_comparison(self.run, self.revision, [hostile])
        before = copy.deepcopy(self.packet)
        html = render_comparison(self.run, self.revision, self.packet)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("default-src 'none'", html)
        self.assertEqual(before, self.packet)

    def test_absent_candidate_is_not_automatic_deletion_of_every_claim(self):
        self.revision.update(
            candidate_answer=None, applied=False, status="rejected", errors=["revision_timeout"]
        )
        self.packet = prepare_comparison(self.run, self.revision)
        self.assertTrue(self.check()["valid"])
        self.assertIsNone(self.packet["candidate"])
        self.assertTrue(all(m["relation"] == "pending" for m in self.packet["mappings"]))

    def test_incomplete_visible_text_does_not_become_candidate_claims(self):
        self.revision.update(
            status="rejected", applied=False, errors=["revision_completion_unverified"]
        )
        self.packet = prepare_comparison(self.run, self.revision)
        self.assertTrue(self.check()["valid"])
        self.assertIsNone(self.packet["candidate"])
        self.assertIn("仅用于故障诊断", render_comparison(self.run, self.revision, self.packet))

    def test_malformed_packet_is_a_validation_failure_not_a_crash(self):
        for bad in (
            None,
            [],
            {},
            {**self.packet, "mappings": [None]},
            {**self.packet, "candidate": 42},
        ):
            self.assertFalse(check_comparison(self.run, self.revision, bad)["valid"])
