import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from as_researcher.core import ResearchConfig
from as_researcher.mcp_bridge import MCPBackends
from as_researcher.models import ScriptedModel
from as_researcher.pages import PageReader
from as_researcher.researcher import ResearchSession, run_research
from as_researcher.review import check_review, prepare_review
from as_researcher.search import FixtureSearch
from as_researcher.segments import parse_document, select_passages


class PassageTests(unittest.TestCase):
    def test_nested_heading_markup_does_not_lose_heading_context(self):
        doc = parse_document(
            "<h2><div><span>stdio</span></div></h2><p>The client starts a child process.</p>"
        )
        self.assertEqual(doc["passages"][-1]["heading_path"], ["stdio"])
        self.assertIn("child process", select_passages(doc, "stdio", 100)["content"])

    def test_toc_backref_heading_and_definition_scope_are_preserved(self):
        doc = parse_document(
            '<h2><a class="toc-backref">Timeouts</a></h2><dl><dt>asyncio.wait_for()</dt><dd><p>Wait for cancellation completion.</p></dd></dl><p>Outside definition.</p>'
        )
        body = next(p for p in doc["passages"] if p["text"].startswith("Wait for"))
        self.assertEqual(body["heading_path"], ["Timeouts", "asyncio.wait_for()"])
        self.assertEqual(doc["passages"][-1]["heading_path"], ["Timeouts"])
        self.assertIn("cancellation completion", select_passages(doc, "wait_for", 100)["content"])

    def test_navigation_hidden_and_inline_identifiers(self):
        doc = parse_document(
            '<div class="toc"><p>noise</p></div><h1>Guide</h1><p><span>asyncio</span>.<code>timeout</code> cancels.</p><p hidden>secret</p>'
        )
        self.assertEqual(doc["text"], "Guide\n\nasyncio.timeout cancels.")
        self.assertEqual(doc["passages"][1]["heading_path"], ["Guide"])

    def test_definition_beats_repeated_link_index_and_offsets_roundtrip(self):
        html = (
            "<h1>Reference</h1><div>"
            + '<p><a href="#x">timeout</a></p>' * 20
            + "</div><h2>timeout</h2><p>The task is cancelled at the deadline.</p>"
        )
        doc = parse_document(html)
        result = select_passages(doc, "timeout", 100)
        self.assertIn("cancelled", result["content"])
        self.assertLessEqual(len(result["content"]), 100)
        for segment in result["segments"]:
            self.assertEqual(segment["text"], doc["text"][segment["start"] : segment["end"]])
        self.assertEqual(result, select_passages(parse_document(html), "timeout", 100))

    def test_long_blocks_and_unicode_budget(self):
        doc = parse_document("<h1>测试</h1><p>" + "证据ß" * 1000 + "</p>")
        self.assertTrue(all(len(p["text"]) <= 900 for p in doc["passages"]))
        result = select_passages(doc, "证据", 110)
        self.assertLessEqual(len(result["content"]), 110)
        self.assertNotEqual(
            doc["document_sha256"], parse_document("<p>changed</p>")["document_sha256"]
        )

    def test_empty_focus_falls_back_in_document_order(self):
        doc = parse_document("<p>First.</p><p>Second.</p>")
        self.assertIn("First.", select_passages(doc, "", 100)["content"])


class EvidenceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_metadata_survives_session_without_duplicate_model_text(self):
        async def html(url):
            return "<h1>Memory</h1><p>Evidence supports a claim.</p>"

        with patch("as_researcher.pages.fetch_html", html):
            session = ResearchSession(ResearchConfig(), FixtureSearch(), PageReader())
            await session.search_web("memory")
            source = next(iter(session.sources.values()))
            response = await session.read_page(source.id, "claim")
        saved = session.page_evidence[source.id]
        self.assertTrue(saved["segments"])
        self.assertIn("text", saved["segments"][0])
        self.assertNotIn("segments", json.loads(response.content[0].text))
        self.assertEqual(saved["id"], source.id)

    async def test_answer_limit_flags_without_truncating_and_fingerprints_prompt(self):
        result = await run_research(
            "test",
            ScriptedModel(),
            FixtureSearch(),
            ResearchConfig(max_answer_chars=100),
            mode="offline_fixture",
        )
        self.assertFalse(result["metrics"]["answer_within_limit"])
        self.assertGreater(result["metrics"]["answer_chars"], 100)
        self.assertEqual(len(result["provenance"]["source_sha256"]), 64)
        other = await run_research("test", ScriptedModel(), FixtureSearch(), mode="offline_fixture")
        self.assertNotEqual(
            result["provenance"]["prompt_sha256"], other["provenance"]["prompt_sha256"]
        )

    async def test_mcp_errors_and_cancellation_are_logged_without_messages(self):
        remote = MCPBackends("fixture", "fixture")

        async def fail(**kwargs):
            raise ConnectionError("secret-api-key must never be logged")

        with self.assertRaises(ConnectionError):
            await remote._call(SimpleNamespace(name="fake", call=fail))

        async def cancelled(**kwargs):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await remote._call(SimpleNamespace(name="fake", call=cancelled))
        self.assertEqual([c["call_id"] for c in remote.calls], [1, 2])
        self.assertEqual(remote.calls[1]["status"], "cancelled")
        self.assertNotIn("secret-api-key", json.dumps(remote.calls))
        self.assertIn("duration_ms", remote.calls[0])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            "answer": "客户端启动子进程[SRC-12345678]。无可核验信息。",
            "sources": [{"id": "SRC-12345678", "content": "The client launches a subprocess."}],
            "page_evidence": [],
        }

    def reviewed(self):
        packet = prepare_review(self.run)
        packet["reviewer"] = {"kind": "assistant", "name": "test"}
        packet["units"][0].update(
            label="supported",
            reason="Direct evidence",
            evidence=[
                {"source_id": "SRC-12345678", "kind": "snippet", "quote": "launches a subprocess"}
            ],
        )
        return packet

    def test_assistant_judgments_never_produce_human_score(self):
        result = check_review(self.run, self.reviewed())
        self.assertTrue(result["valid"])
        self.assertEqual(result["reviewed_units"], 1)
        self.assertIsNone(result["human_support_label_rate"])

    def test_tampered_evidence_or_run_fails(self):
        packet = self.reviewed()
        packet["units"][0]["evidence"][0]["quote"] = "invented"
        self.assertFalse(check_review(self.run, packet)["valid"])
        packet = self.reviewed()
        changed = {**self.run, "answer": "Different answer"}
        self.assertFalse(check_review(changed, packet)["valid"])

    def test_dropped_unit_and_unlinked_support_fail(self):
        packet = self.reviewed()
        packet["units"].pop()
        self.assertFalse(check_review(self.run, packet)["valid"])
        packet = self.reviewed()
        packet["units"][0]["evidence"][0]["source_id"] = "SRC-UNKNOWN"
        self.assertFalse(check_review(self.run, packet)["valid"])

    def test_pending_is_valid_draft_not_complete_score(self):
        packet = prepare_review(self.run)
        self.assertTrue(check_review(self.run, packet)["valid"])
        self.assertEqual(check_review(self.run, packet)["reviewed_units"], 0)

    def test_human_score_requires_complete_coverage_declaration(self):
        packet = self.reviewed()
        packet["reviewer"]["kind"] = "human"
        packet["units"][1].update(label="not_factual", reason="Refusal")
        self.assertIsNone(check_review(self.run, packet)["human_support_label_rate"])
        packet["coverage_checked"] = True
        self.assertEqual(check_review(self.run, packet)["human_support_label_rate"], 1.0)
        changed = copy.deepcopy(packet)
        changed["units"][0]["text"] = "Changed claim"
        self.assertFalse(check_review(self.run, changed)["valid"])


if __name__ == "__main__":
    unittest.main()
