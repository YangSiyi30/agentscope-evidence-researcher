"""Final-release regressions: HTTP diagnostics, atomic evidence and URL edges."""

import asyncio
import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from agentscope.credential import OpenAICredential

from as_researcher.cli import main
from as_researcher.core import ResearchConfig, Source, canonical_url
from as_researcher.evidence import reader_page_data
from as_researcher.mcp_bridge import MCPBackendError, MCPBackends
from as_researcher.mcp_server import read_public_page
from as_researcher.models import ObservedOpenAIChatModel, ScriptedModel
from as_researcher.pages import PageReadError
from as_researcher.researcher import ResearchSession, render_report, run_research
from as_researcher.review import check_review, prepare_review
from as_researcher.source_inputs import normalize_source_urls


def completion():
    return {
        "id": "fixture",
        "object": "chat.completion",
        "created": 1,
        "model": "fixture",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "证据不足。"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class FinalModelTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, statuses, retries=0, reuse=False):
        requests = []

        async def handler(request):
            requests.append(request)
            status = statuses[min(len(requests) - 1, len(statuses) - 1)]
            return httpx.Response(
                status,
                json=completion()
                if status == 200
                else {
                    "error": {
                        "message": "secret-body-marker",
                        "type": "secret-type-marker",
                        "code": "secret-code-marker",
                        "param": "secret-param-marker",
                    }
                },
                headers={"x-secret": "secret-header-marker"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = ObservedOpenAIChatModel(
                credential=OpenAICredential(
                    api_key="secret-key-marker", base_url="https://model.invalid/v1"
                ),
                model="fixture",
                stream=False,
                max_retries=retries,
                client_kwargs={"http_client": client, "max_retries": 0},
            )
            run = await run_research("test", model, lambda *_: None, mode="mock_http")
            if reuse:
                run = await run_research("next", model, lambda *_: None, mode="mock_http")
        self.assertNotIn("secret-", json.dumps(run))
        return run, requests

    async def test_status_errors_are_safe_and_no_response_is_not_zero_attempts(self):
        for status, category in [
            (400, "model_invalid_request"),
            (401, "model_authentication_error"),
            (403, "model_permission_denied"),
            (404, "model_not_found"),
            (429, "model_rate_limited"),
            (500, "model_server_error"),
        ]:
            with self.subTest(status=status):
                run, requests = await self.exercise([status])
                self.assertEqual(len(requests), 1)
                self.assertEqual(run["metrics"]["model_request_attempts"], 1)
                self.assertEqual(run["model_requests"][0]["http_status"], status)
                self.assertEqual(run["model_requests"][0]["error_code"], category)
                self.assertEqual(run["provider_responses"], [])
                self.assertIsNone(run["metrics"]["token_usage"])
                self.assertIn(category, render_report(run))

    async def test_failed_attempt_then_success_does_not_claim_complete_token_total(self):
        with self.assertLogs("as", level="WARNING") as logs:
            run, requests = await self.exercise([429, 200], retries=1)
        self.assertNotIn("secret-", " ".join(logs.output))
        self.assertIn("sanitized model_requests", " ".join(logs.output))
        self.assertEqual(len(requests), 2)
        self.assertEqual(run["metrics"]["status"], "completed")
        self.assertEqual(run["metrics"]["model_request_attempts"], 2)
        self.assertEqual(run["metrics"]["model_request_failures"], 1)
        self.assertIsNone(run["metrics"]["token_usage"])
        self.assertEqual(run["provider_responses"][0]["output_tokens"], 5)

    async def test_request_history_is_scoped_to_each_run(self):
        run, requests = await self.exercise([400, 200], reuse=True)
        self.assertEqual(len(requests), 2)
        self.assertEqual(run["model_requests"][0]["request_index"], 2)
        self.assertEqual(run["metrics"]["model_request_attempts"], 1)
        self.assertEqual(run["metrics"]["token_usage"], {"input": 10, "output": 5})

    async def test_cancelled_request_is_recorded_without_body_or_retry(self):
        async def handler(request):
            raise asyncio.CancelledError

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = ObservedOpenAIChatModel(
                credential=OpenAICredential(api_key="fixture", base_url="https://model.invalid"),
                model="fixture",
                stream=False,
                max_retries=0,
                client_kwargs={"http_client": client, "max_retries": 0},
            )
            run = await run_research("test", model, lambda *_: None, mode="mock_http")
        self.assertEqual(len(run["model_requests"]), 1)
        self.assertEqual(run["model_requests"][0]["status"], "cancelled")
        self.assertIsNone(run["metrics"]["token_usage"])


class AtomicEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def session(self, payload):
        source = Source("fixture", "https://example.org/a", "snippet")

        async def search(*_):
            return [source]

        async def reader(*_, **__):
            return payload

        session = ResearchSession(ResearchConfig(max_searches=1, max_page_reads=1), search, reader)
        await session.search_web("test")
        return session, source

    async def test_bad_metadata_never_leaves_saved_page_on_error(self):
        for extra in [
            {"selection_warnings": None, "code_selection": {}},
            {"code_selection": []},
            {"segments": ["bad"]},
            {"document_chars": True},
            {"segments": [{"bad": float("nan")}]},
            {"strategy": object()},
            {"selection_warnings": ["x" * 128001]},
        ]:
            with self.subTest(extra=str(extra)[:60]):
                session, source = await self.session({"content": "body", **extra})
                result = json.loads((await session.read_page(source.id)).content[0].text)
                self.assertEqual(result["error"], "invalid_page_metadata")
                self.assertEqual(session.page_attempts, 1)
                self.assertEqual(session.page_evidence, {})
                self.assertEqual(result["pages_read"], 0)

    async def test_reader_metadata_is_copied_and_unknown_error_text_not_exposed(self):
        page = {"content": "body", "selection_warnings": [], "segments": [{"text": "body"}]}
        session, source = await self.session(page)
        await session.read_page(source.id)
        page["segments"][0]["text"] = "changed"
        self.assertEqual(session.page_evidence[source.id]["segments"][0]["text"], "body")

        async def bad(*_, **__):
            raise PageReadError("secret-reader-marker")

        session.reader = bad
        session.read_ids.clear()
        session.page_attempts = 0
        result = await session.read_page(source.id)
        self.assertNotIn("secret-reader", result.content[0].text)
        self.assertEqual(json.loads(result.content[0].text)["error"], "page_reader_error")

    async def test_bad_search_batch_never_commits_a_partial_source(self):
        source = Source("fixture", "https://example.org/a", "snippet")

        async def search(*_):
            return [source, Source(None, "https://example.org/b", "body")]

        session = ResearchSession(ResearchConfig(max_searches=1), search)
        result = json.loads((await session.search_web("test")).content[0].text)
        self.assertEqual(result["error_code"], "search_invalid_response")
        self.assertEqual(session.sources, {})
        self.assertEqual(session.attempts, 1)

    def test_metadata_cycles_and_oversized_trees_are_rejected(self):
        cycle = []
        cycle.append(cycle)
        for meta in [cycle, [{}] * 4100]:
            with self.assertRaises(ValueError):
                reader_page_data({"content": "body", "code_selection": {"nested": meta}})

    def test_zero_port_is_not_silently_changed_to_the_default_port(self):
        self.assertEqual(canonical_url("https://example.org:0/"), "https://example.org:0/")
        with self.assertRaises(ValueError):
            normalize_source_urls(["https://example.org:0/"])

    async def test_network_page_errors_match_local_server_and_bridge_observations(self):
        request = httpx.Request("GET", "https://example.org/a")
        for exc, code in [
            (TimeoutError("secret-timeout"), "page_timeout"),
            (httpx.ConnectTimeout("secret-timeout"), "page_timeout"),
            (httpx.ConnectError("secret-connect"), "page_connection_error"),
            (OSError("secret-dns"), "page_connection_error"),
            (
                httpx.HTTPStatusError(
                    "secret-http", request=request, response=httpx.Response(403, request=request)
                ),
                "page_http_error",
            ),
            (PageReadError("secret-custom"), "page_reader_error"),
        ]:
            with self.subTest(code=code):
                reader = AsyncMock(side_effect=exc)
                with (
                    patch("as_researcher.mcp_server.PageReader", return_value=reader),
                    patch.dict("os.environ", {"RESEARCH_READER": "http"}),
                ):
                    backend_result = await read_public_page("https://example.org/a")
                self.assertEqual(backend_result, {"error": code})
                remote = MCPBackends("fixture", "http")
                remote.page_tool = object()
                remote._call = AsyncMock(side_effect=MCPBackendError(code))
                outputs = []
                for backend in (reader, remote.read):
                    session = ResearchSession(
                        ResearchConfig(max_searches=0, max_page_reads=1),
                        lambda *_: None,
                        backend,
                        source_urls=["https://example.org/a"],
                    )
                    result = json.loads(
                        (await session.read_page(next(iter(session.sources.values())).id))
                        .content[0]
                        .text
                    )
                    self.assertEqual(result["error"], code)
                    self.assertEqual(result["page_read_attempts"], 1)
                    self.assertEqual(result["pages_read"], 0)
                    self.assertNotIn("secret-", json.dumps(result))
                    outputs.append(result)
                self.assertEqual(outputs[0], outputs[1])


class EditableReviewTests(unittest.TestCase):
    def test_malformed_legacy_packet_is_rejected_without_crashing(self):
        run = {"answer": "保留原始回答。", "sources": [], "page_evidence": []}
        original = prepare_review(run)
        for field, value in [("reviewer", []), ("units", [None]), ("units", {})]:
            packet = copy.deepcopy(original)
            packet[field] = value
            self.assertFalse(check_review(run, packet)["valid"])
        for field, value in [
            ("unit_id", []),
            ("label", []),
            ("reason", None),
            ("citation_ids", [[]]),
            ("evidence", [None]),
        ]:
            packet = copy.deepcopy(original)
            packet["units"][0][field] = value
            self.assertFalse(check_review(run, packet)["valid"])
        packet = copy.deepcopy(original)
        packet["units"][0].update(
            label="supported", evidence=[{"source_id": [], "kind": "snippet", "quote": "body"}]
        )
        self.assertEqual(check_review(run, packet)["errors"], ["invalid_evidence_source"])

    def test_unassigned_legacy_draft_remains_valid_but_not_human_gold(self):
        run = {"answer": "保留原始回答。", "sources": [], "page_evidence": []}
        result = check_review(run, prepare_review(run))
        self.assertTrue(result["valid"])
        self.assertIsNone(result["human_support_label_rate"])


class MainThinkingTests(unittest.TestCase):
    def test_cli_passes_main_thinking_and_closes_the_client(self):
        model = ScriptedModel()
        model.client = SimpleNamespace(close=AsyncMock())
        with (
            patch(
                "sys.argv",
                [
                    "as-research",
                    "test",
                    "--live",
                    "--search-provider",
                    "fixture",
                    "--model-thinking",
                    "disabled",
                ],
            ),
            patch("as_researcher.cli.load_dotenv"),
            patch("as_researcher.cli.live_model", return_value=model) as factory,
            patch("as_researcher.cli.save_run", return_value=Path("reports/fixture")) as save,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(factory.call_args.kwargs["thinking_mode"], "disabled")
        self.assertEqual(save.call_args.args[0]["model_thinking"], "disabled")
        model.client.close.assert_awaited_once()

    def test_thinking_opt_in_without_live_is_rejected_before_loading_environment(self):
        with (
            patch("sys.argv", ["as-research", "--model-thinking", "disabled"]),
            patch("as_researcher.cli.load_dotenv") as load,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main()
        load.assert_not_called()
