"""Reader metadata must not redefine application identity, state or tool observations."""

import copy
import json
import unittest
from pathlib import Path

from as_researcher.core import ResearchConfig, Source
from as_researcher.pages import PageReadError
from as_researcher.researcher import ResearchSession
from as_researcher.segments import parse_document, select_passages
from scripts.observe_reader_boundary import inspect_native, observe_direct, observe_native

ROOT = Path(__file__).resolve().parents[1]


class ReaderBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_identity_and_trust_cannot_replace_application_fields(self):
        row = (await observe_direct())["rows"][0]
        self.assertTrue(row["checks"]["source_identity_owned_by_application"])
        self.assertTrue(row["checks"]["tool_evidence_remains_untrusted"])
        self.assertTrue(row["checks"]["reader_control_fields_not_saved_or_forwarded"])
        self.assertEqual(row["page_reply"]["content"], row["reader_return"]["content"])

    async def test_reader_controls_do_not_label_saved_body_as_failure_or_fake_usage(self):
        row = (await observe_direct())["rows"][1]
        self.assertTrue(row["passed"], row["checks"])
        self.assertEqual(row["page_trace"]["status"], "ok")
        self.assertEqual(row["retry_reply"]["error"], "already_attempted")
        self.assertEqual(len(row["reader_calls"]), 1)

    async def test_invalid_body_or_real_reader_exception_still_consumes_attempt(self):
        source = Source("fixture", "https://example.org/reader", "snippet")
        for value, error in [
            ({"error": "synthetic_reader_error"}, "invalid_page_payload"),
            ({"content": " ", "error": "synthetic_reader_error"}, "empty_page_excerpt"),
            ({"content": "x" * 301, "error": "synthetic_reader_error"}, "page_budget_exceeded"),
            (PageReadError("empty_page"), "empty_page"),
        ]:
            with self.subTest(error=error):

                async def reader(*args, **kwargs):
                    if isinstance(value, Exception):
                        raise value
                    return copy.deepcopy(value)

                session = ResearchSession(ResearchConfig(chars_per_page=300), None, reader)
                session.sources[source.url] = source
                data = json.loads((await session.read_page(source.id)).content[0].text)
                self.assertEqual(data["error"], error)
                self.assertEqual(session.trace[-1]["status"], "error")
                self.assertTrue(session.trace[-1]["backend_attempted"])
                self.assertEqual(session.page_attempts, 1)
                self.assertFalse(session.page_evidence)
                self.assertIn(source.id, session.read_ids)

    async def test_normal_segments_offsets_and_omission_metadata_survive_without_mutation(self):
        fixture = json.loads((ROOT / "eval/omission_v08.json").read_bytes())
        original = select_passages(parse_document(fixture["html"]), fixture["focus"], 500)
        before = copy.deepcopy(original)
        source = Source("fixture", "https://example.org/segments", "snippet")

        async def reader(*args, **kwargs):
            return original

        session = ResearchSession(ResearchConfig(chars_per_page=500), None, reader)
        session.sources[source.url] = source
        observation = json.loads((await session.read_page(source.id)).content[0].text)
        saved = session.page_evidence[source.id]
        for key, value in original.items():
            self.assertEqual(saved[key], value, key)
        self.assertEqual(saved["code_selection"], observation["code_selection"])
        self.assertNotIn("segments", observation)
        self.assertNotIn("document_sha256", observation)
        self.assertNotIn("omitted_code_blocks", observation)
        self.assertEqual(original, before)
        self.assertEqual(session.trace[-1]["status"], "ok")

    async def test_native_model_gets_authoritative_page_consistent_with_saved_trace(self):
        run, observation = await observe_native()
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(run["metrics"]["pages_read"], 1)
        self.assertEqual(observation["actual_mcp_rpcs"], 0)

    async def test_checker_rejects_spoofed_received_source_despite_correct_saved_trace(self):
        run, observation = await observe_native()
        turns = copy.deepcopy(observation["model_turns"])
        page = next(b for b in turns[2]["received_tool_results"] if b["name"] == "read_page")
        data = json.loads(page["output"][0]["text"])
        data["source_id"] = "SRC-00000000"
        page["output"][0]["text"] = json.dumps(data)
        checked = inspect_native(run, turns, observation["reader_calls"])
        self.assertFalse(checked["passed"])
        self.assertFalse(checked["checks"]["model_and_saved_source_identity_agree"])
        self.assertEqual(run["trace"][-1]["status"], "ok")
        self.assertTrue(observation["passed"])


if __name__ == "__main__":
    unittest.main()
