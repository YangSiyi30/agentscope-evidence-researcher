"""No live service. Verify preflight, actual SDK payload, and old artifact compatibility."""

import copy
import json
import unittest

from as_researcher.revision import (
    build_revision_input,
    digest,
    revision_integrity_errors,
    revision_length_budget,
)
from scripts.observe_revision_budget import exercise, received_input, synthetic_sources


class RevisionBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_impossible_distinct_citations_skip_even_model_invocation(self):
        sources = synthetic_sources()
        citations = "".join(f"[{s['id']}@snippet]" for s in sources)
        record, requests = await exercise("test", "说明" + citations, sources, [], 100, citations)
        self.assertEqual(requests, [])
        self.assertEqual(record["attempts"], 0)
        self.assertEqual(record["errors"], ["revision_citation_budget_impossible"])
        self.assertEqual(record["length_budget"]["minimum_citation_chars"], 132)
        self.assertNotIn("request", record)
        self.assertIsNone(record["token_usage"])
        self.assertFalse(record["applied"])
        self.assertEqual(revision_integrity_errors(record, record["original_answer"]), [])

    async def test_repeated_citations_do_not_trigger_impossible_guard_and_feedback_reaches_sdk(
        self,
    ):
        source = synthetic_sources()[0]
        ref = f"[{source['id']}@snippet]"
        original = ("事实 " + ref + "\n") * 8
        candidate = "合并同一证据支持的事实" + ref
        record, requests = await exercise("test", original, [source], [], 100, candidate)
        self.assertTrue(record["applied"])
        self.assertEqual(len(requests), 1)
        self.assertIsInstance(requests[0]["messages"][-1]["content"], list)
        sent = received_input(requests[0])
        self.assertEqual(sent["length_budget"], record["length_budget"])
        self.assertEqual(sent["length_budget"]["citation_chars"], 176)
        self.assertEqual(sent["length_budget"]["minimum_citation_chars"], 22)
        self.assertEqual(sent["saved_evidence"][0]["content"], source["content"])
        self.assertEqual(requests[0]["tool_choice"], "none")
        self.assertFalse(requests[0].get("tools"))
        self.assertEqual(revision_integrity_errors(record, candidate), [])
        tampered = copy.deepcopy(record)
        tampered["length_budget"]["original_chars"] += 1
        self.assertIn("revision_length_budget_mismatch", revision_integrity_errors(tampered))
        tampered = copy.deepcopy(record)
        payload = json.loads(tampered["request"]["user"])
        payload["length_budget"]["excess_chars"] = 0
        tampered["request"]["user"] = json.dumps(payload)
        tampered["input_sha256"] = digest(tampered["request"]["user"])
        self.assertIn("request_length_budget_mismatch", revision_integrity_errors(tampered))

    async def test_equal_lower_bound_does_not_claim_semantic_feasibility(self):
        sources = synthetic_sources()
        citations = "".join(f"[{s['id']}@snippet]" for s in sources)
        record, requests = await exercise("test", "说明" + citations, sources, [], 132, citations)
        self.assertEqual(len(requests), 1)
        self.assertTrue(record["applied"])
        self.assertEqual(record["length_budget"]["remaining_after_minimum_citations"], 0)
        self.assertEqual(record["semantic_review"], "required_not_automated")

    async def test_reference_changes_still_reject_and_old_records_still_validate(self):
        source = synthetic_sources()[0]
        original = ("事实" + f"[{source['id']}@snippet]") * 8
        record, requests = await exercise("test", original, [source], [], 100, "删除引用的短稿")
        self.assertEqual(len(requests), 1)
        self.assertFalse(record["applied"])
        self.assertIn("revision_citation_set_changed", record["errors"])
        self.assertEqual(revision_integrity_errors(record, original), [])
        old = copy.deepcopy(record)
        old.pop("length_budget")
        payload = json.loads(old["request"]["user"])
        payload.pop("length_budget")
        old["request"]["user"] = json.dumps(payload)
        old["input_sha256"] = digest(old["request"]["user"])
        self.assertEqual(revision_integrity_errors(old, original), [])

    def test_nonwhitespace_counts_and_both_layers_are_distinct(self):
        text = "甲\n [SRC-12345678@page]\t[SRC-12345678@page] [SRC-12345678@snippet]"
        budget = revision_length_budget(text, 100)
        self.assertEqual(budget["original_chars"], 61)
        self.assertEqual(budget["citation_occurrences"], 3)
        self.assertEqual(budget["distinct_citations"], 2)
        self.assertEqual(budget["citation_chars"], 60)
        self.assertEqual(budget["minimum_citation_chars"], 41)
        self.assertEqual(budget["noncitation_chars"], 1)
        self.assertEqual(budget["excess_chars"], 0)
        payload = json.loads(build_revision_input("test", text, [], [], 100))
        self.assertEqual(payload["length_budget"], budget)
        self.assertEqual(payload["original_answer"], text)
