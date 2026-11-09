"""Field/level contracts using saved fixtures, never semantic accuracy scores."""

import copy
import json
import unittest
from pathlib import Path

from as_researcher.atomic_review import prepare_atomic_review
from as_researcher.evidence import evidence_diagnostics, quote_evidence_text, typed_refs
from as_researcher.review import check_review, prepare_review
from as_researcher.revision import answer_chars, build_revision_input
from as_researcher.worksheet import render_worksheet

SID = "SRC-12345678"


class TitleEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            "answer": f"搜索标题为Sable v2.1。[{SID}@snippet]",
            "sources": [
                {"id": SID, "title": "Sable v2.1 reference", "content": "Task reference summary."}
            ],
            "page_evidence": [
                {
                    "id": SID,
                    "content": "A task body, no version.",
                    "segments": [{"segment_id": "SEG-1", "text": "task body"}],
                }
            ],
        }

    def packet(self, prepare, evidence, answer=None):
        run = {**self.run, "answer": answer or self.run["answer"]}
        packet = prepare(run)
        packet["reviewer"] = {"kind": "assistant", "name": "fixture reviewer"}
        unit = packet["units"][0]
        claim = unit.get("claims", [unit])[0]
        claim.update(
            label="supported",
            evidence=[evidence],
            reason="Only a saved-title statement, no page-version inference.",
        )
        return run, packet

    def test_explicit_title_works_in_both_schemas_without_mutating_content(self):
        before = copy.deepcopy(self.run)
        for prepare in (prepare_review, prepare_atomic_review):
            run, packet = self.packet(
                prepare,
                {"source_id": SID, "kind": "snippet", "field": "title", "quote": "Sable v2.1"},
            )
            check = check_review(run, packet)
            self.assertTrue(check["valid"], check["errors"])
            self.assertIsNone(check["human_support_label_rate"])
            self.assertIn('"field": "title"', render_worksheet(run, packet))
        self.assertEqual(self.run, before)

    def test_omitted_field_means_content_not_auto_title_fallback(self):
        for prepare in (prepare_review, prepare_atomic_review):
            run, packet = self.packet(
                prepare, {"source_id": SID, "kind": "snippet", "quote": "Sable v2.1"}
            )
            self.assertFalse(check_review(run, packet)["valid"])
            run, packet = self.packet(
                prepare, {"source_id": SID, "kind": "snippet", "quote": "Task reference summary."}
            )
            self.assertTrue(check_review(run, packet)["valid"])
            # Valid quote bookkeeping does not certify this version claim.

    def test_title_cannot_support_page_reference_even_for_same_source(self):
        for prepare in (prepare_review, prepare_atomic_review):
            run, packet = self.packet(
                prepare,
                {"source_id": SID, "kind": "snippet", "field": "title", "quote": "Sable v2.1"},
                answer=f"正文版本已核验。[{SID}@page]",
            )
            result = check_review(run, packet)
            self.assertFalse(result["valid"])
            self.assertTrue(any("level_mismatch" in e for e in result["errors"]))

    def test_wrong_field_kind_segment_and_types_rejected(self):
        variants = [
            {"kind": "page", "field": "title"},
            {"kind": "snippet", "field": "url"},
            {"kind": "snippet", "field": None},
            {"kind": "snippet", "field": []},
            {"kind": "snippet", "field": "title", "segment_id": "SEG-1"},
            {"kind": "snippet", "segment_id": "SEG-1"},
            {"kind": "page", "segment_id": 42},
        ]
        for prepare in (prepare_review, prepare_atomic_review):
            for location in variants:
                with self.subTest(schema=prepare.__name__, location=location):
                    run, packet = self.packet(
                        prepare, {"source_id": SID, "quote": "Sable v2.1", **location}
                    )
                    result = check_review(run, packet)
                    self.assertFalse(result["valid"])
                    self.assertTrue(any("invalid_evidence_location" in e for e in result["errors"]))

    def test_missing_title_does_not_borrow_other_source_or_body(self):
        self.run["sources"][0].pop("title")
        self.run["sources"][0]["content"] = "Sable v2.1 reference"
        self.run["sources"].append(
            {"id": "SRC-87654321", "title": "Sable v2.1 reference", "content": ""}
        )
        for prepare in (prepare_review, prepare_atomic_review):
            run, packet = self.packet(
                prepare,
                {"source_id": SID, "kind": "snippet", "field": "title", "quote": "Sable v2.1"},
            )
            self.assertFalse(check_review(run, packet)["valid"])

    def test_old_page_segment_default_and_explicit_content_are_equivalent(self):
        item = {"source_id": SID, "kind": "page", "segment_id": "SEG-1"}
        self.assertEqual(quote_evidence_text(self.run, item), "task body")
        self.assertEqual(quote_evidence_text(self.run, {**item, "field": "content"}), "task body")
        self.assertEqual(quote_evidence_text(self.run, {**item, "segment_id": "unknown"}), "")

    def test_frozen_synthetic_cases_expose_semantic_limits_and_one_character_budget(self):
        fixture = json.loads(
            (Path(__file__).resolve().parents[1] / "eval/revision_semantics_v011.json").read_text(
                "utf-8"
            )
        )
        for case in fixture["cases"]:
            sources = [{"id": SID, "title": case["title"], "content": case["snippet"]}]
            pages = [
                {
                    "id": SID,
                    "content": case["page"],
                    "selection_warnings": case["selection_warnings"],
                }
            ]
            payload = json.loads(
                build_revision_input(
                    case["topic"], case["original"], sources, pages, fixture["max_answer_chars"]
                )
            )
            self.assertEqual(payload["max_answer_chars"], 250)
            self.assertEqual(payload["saved_evidence"][0]["title"], case["title"])
            for key in ("good_candidate", "bad_candidate_same_refs"):
                # Both hand-written responses satisfy mechanics; semantic distinction is NOT tested by these checks.
                self.assertLessEqual(answer_chars(case[key]), fixture["max_answer_chars"])
                self.assertEqual(set(typed_refs(case[key])), set(typed_refs(case["original"])))
                self.assertTrue(evidence_diagnostics(case[key], sources, pages)["contract_valid"])
