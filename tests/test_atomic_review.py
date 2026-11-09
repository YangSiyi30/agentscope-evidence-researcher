import copy
import unittest

from as_researcher.atomic_review import prepare_atomic_review
from as_researcher.review import check_review


class AtomicReviewTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            "answer": "# 标题\r\n😀甲成立，乙成立。[SRC-12345678]\n\n未知。",
            "sources": [{"id": "SRC-12345678", "content": "甲成立。乙成立。"}],
            "page_evidence": [
                {
                    "id": "SRC-12345678",
                    "content": "甲成立。",
                    "segments": [{"segment_id": "SEG-1", "text": "甲成立。"}],
                }
            ],
        }
        self.packet = prepare_atomic_review(self.run)

    def check(self):
        return check_review(self.run, self.packet)

    def reviewed(self, kind="assistant"):
        self.packet["reviewer"] = {"kind": kind, "name": "test reviewer"}
        self.packet.update(coverage_checked=True, atomicity_checked=True)
        for unit in self.packet["units"]:
            unit["claims"][0].update(label="not_factual", reason="展示标题或未知声明")
        claim = self.packet["units"][1]["claims"][0]
        claim.update(
            label="supported",
            statement="甲成立，乙成立",
            reason="两项声明需要拆分复核",
            evidence=[
                {"source_id": "SRC-12345678", "kind": "snippet", "quote": "甲成立。乙成立。"}
            ],
        )
        return claim

    def test_draft_keeps_heading_unicode_and_full_answer_without_score(self):
        result = self.check()
        self.assertTrue(result["valid"])
        self.assertEqual(len(self.packet["units"]), 3)
        self.assertEqual(self.packet["answer"], self.run["answer"])
        self.assertEqual(result["covered_nonblank_chars"], result["total_nonblank_chars"])
        self.assertFalse(result["review_complete"])
        self.assertIsNone(result["human_support_label_rate"])
        unit = self.packet["units"][1]
        self.assertEqual(self.run["answer"][unit["start"] : unit["end"]], unit["text"])

    def test_shared_context_allows_two_claims_only_with_explicit_reasons(self):
        first = self.reviewed()
        second = copy.deepcopy(first)
        first["statement"] = "甲成立"
        second.update(claim_id="C002.A02", statement="乙成立")
        self.packet["units"][1]["claims"].append(second)
        self.assertIn("C002.A02:unexplained_shared_span", self.check()["errors"])
        first["overlap_reason"] = second["overlap_reason"] = (
            "原句并列两个事实，共用句尾引用；分别核验。"
        )
        result = self.check()
        self.assertTrue(result["valid"])
        self.assertEqual(result["shared_span_pairs"], [["C002.A01", "C002.A02"]])
        self.assertIsNone(result["human_support_label_rate"])

    def test_dropped_claim_or_uncovered_suffix_fails(self):
        claim = self.packet["units"][1]["claims"][0]
        span = claim["spans"][0]
        span["end"] = span["start"] + 3
        span["text"] = self.run["answer"][span["start"] : span["end"]]
        self.assertIn("uncovered_answer_characters", self.check()["errors"])
        self.packet["units"].pop()
        self.assertIn("missing_reordered_or_extra_units", self.check()["errors"])

    def test_text_offset_and_source_tampering_fail(self):
        original = copy.deepcopy(self.packet)
        for mutation in ["text", "start", "citation_ids", "answer"]:
            self.packet = copy.deepcopy(original)
            claim = self.packet["units"][1]["claims"][0]
            if mutation == "text":
                claim["spans"][0]["text"] = "改写原文"
            elif mutation == "start":
                claim["spans"][0]["start"] = -1
            elif mutation == "citation_ids":
                claim["citation_ids"] = ["SRC-UNKNOWN"]
            else:
                self.packet["answer"] += "改写"
            self.assertFalse(self.check()["valid"], mutation)

    def test_saved_page_segment_quote_and_citation_are_required(self):
        claim = self.reviewed()
        claim["evidence"] = [
            {"source_id": "SRC-12345678", "kind": "page", "segment_id": "SEG-1", "quote": "甲成立"}
        ]
        self.assertTrue(self.check()["valid"])
        claim["evidence"][0]["quote"] = "乙成立"
        self.assertFalse(self.check()["valid"])
        claim["evidence"][0]["quote"] = "甲成立"
        claim["citation_ids"] = []
        self.assertFalse(self.check()["valid"])

    def test_not_factual_still_needs_reason(self):
        self.reviewed()
        self.packet["units"][0]["claims"][0]["reason"] = " "
        self.assertFalse(self.check()["valid"])

    def test_human_rate_requires_atomicity_coverage_no_pending_and_valid_packet(self):
        self.reviewed("human")
        self.assertEqual(self.check()["human_support_label_rate"], 1.0)
        self.packet["atomicity_checked"] = False
        self.assertIsNone(self.check()["human_support_label_rate"])
        self.packet["atomicity_checked"] = True
        self.packet["coverage_checked"] = False
        self.assertIsNone(self.check()["human_support_label_rate"])
        self.packet["coverage_checked"] = True
        self.packet["units"][0]["claims"][0]["label"] = "pending"
        self.assertIsNone(self.check()["human_support_label_rate"])

    def test_malformed_packet_is_rejected_without_crash(self):
        for field, value in [
            ("units", [None]),
            ("units", {}),
            ("reviewer", None),
            ("reviewer", {"kind": []}),
        ]:
            with self.subTest(field=field):
                packet = copy.deepcopy(self.packet)
                packet[field] = value
                self.assertFalse(check_review(self.run, packet)["valid"])
        for field, value in [
            ("spans", [None]),
            ("evidence", [None]),
            ("label", []),
            ("claim_id", []),
            ("citation_ids", [[]]),
        ]:
            with self.subTest(field=field):
                packet = copy.deepcopy(self.packet)
                packet["units"][1]["claims"][0][field] = value
                self.assertFalse(check_review(self.run, packet)["valid"])

    def test_duplicate_ids_and_overlapping_spans_within_claim_fail(self):
        claim = self.packet["units"][1]["claims"][0]
        claim["spans"].append(copy.deepcopy(claim["spans"][0]))
        self.assertFalse(self.check()["valid"])
        self.packet["units"][1]["claims"].append(copy.deepcopy(claim))
        self.assertIn("C002:invalid_or_duplicate_claim_id", self.check()["errors"])
