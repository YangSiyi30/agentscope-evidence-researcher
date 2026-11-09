"""Policy and native AgentScope integration tests; no network or API fees."""

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

import httpx
from agentscope.credential import OpenAICredential
from pydantic import ValidationError

from as_researcher.cli import save_run
from as_researcher.core import (
    ResearchConfig,
    Source,
    canonical_url,
    citation_diagnostics,
    source_id,
)
from as_researcher.models import ObservedOpenAIChatModel, ScriptedModel
from as_researcher.researcher import ResearchSession, render_report, run_research
from as_researcher.search import FixtureSearch, TavilySearch

logging.getLogger("as").setLevel(logging.CRITICAL)


class CoreTests(unittest.TestCase):
    def test_url_canonicalization_preserves_meaningful_parameters(self):
        self.assertEqual(
            canonical_url("https://EXAMPLE.org:443/a?id=1&utm_source=test#part"),
            "https://example.org/a?id=1",
        )
        self.assertNotEqual(
            canonical_url("https://example.org/?id=1"), canonical_url("https://example.org/?id=2")
        )

    def test_invalid_source_links_are_rejected(self):
        for url in [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://localhost/a",
            "http://127.0.0.1/a",
            "http://u:p@example.org/",
            "https://example.org:99999/",
            "https://example.org/a\nX",
            "http://[::1]/",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(canonical_url(url))

    def test_citation_metric_handles_missing_and_malformed_ids(self):
        s = Source("test", "https://example.org/", "content")
        self.assertIsNone(citation_diagnostics("No references", [s])["id_validity_rate"])
        d = citation_diagnostics(f"[{s.id}] [SRC-typo] [SRC-00000000]", [s])
        self.assertEqual(d["invalid_ids"], ["SRC-00000000", "SRC-typo"])
        self.assertAlmostEqual(d["id_validity_rate"], 1 / 3)

    def test_configuration_rejects_unbounded_budgets(self):
        for config in [
            {"max_searches": -1},
            {"max_searches": 100},
            {"total_timeout": -1},
            {"results_per_search": 6},
        ]:
            with self.assertRaises(ValidationError):
                ResearchConfig(**config)


class NativeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, scenario="normal", **config):
        model, backend = ScriptedModel(scenario), FixtureSearch(scenario)
        result = await run_research(
            "Agent memory?", model, backend, ResearchConfig(**config), mode="offline_fixture"
        )
        return result, model, backend

    async def test_native_agent_calls_tool_and_returns_cited_report(self):
        result, model, backend = await self.run_case()
        self.assertEqual(result["metrics"]["status"], "completed")
        self.assertEqual(model.calls, 3)
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(result["metrics"]["citations"]["id_validity_rate"], 1)
        self.assertIn("离线模拟", render_report(result))

    async def test_final_budget_needs_only_one_final_answer_call(self):
        result, model, backend = await self.run_case(max_searches=1)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(model.calls, 2)
        self.assertEqual(result["metrics"]["stop_reason"], "search_budget")

    async def test_duplicate_query_does_not_reach_backend(self):
        result, _, backend = await self.run_case("duplicate")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(result["metrics"]["tool_invocations"], 2)
        self.assertEqual(result["metrics"]["stop_reason"], "duplicate_query")

    async def test_duplicate_ablation_still_has_no_new_evidence_guard(self):
        result, _, backend = await self.run_case("duplicate", early_stop_duplicates=False)
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(result["metrics"]["stop_reason"], "no_new_evidence")

    async def test_empty_and_unsearched_answers_are_flagged(self):
        for scenario in ["empty", "no_tool"]:
            result, _, _ = await self.run_case(scenario)
            self.assertEqual(result["metrics"]["unique_sources"], 0)
            self.assertTrue(result["metrics"]["warnings"])
            self.assertIsNone(result["metrics"]["citations"]["id_validity_rate"])

    async def test_unknown_citations_are_not_hidden_by_bibliography(self):
        result, _, _ = await self.run_case("invalid_citation")
        self.assertEqual(result["metrics"]["citations"]["invalid_ids"], ["SRC-00000000"])
        self.assertTrue(result["metrics"]["warnings"])

    async def test_search_error_is_auditable_without_exception_body(self):
        async def fail(query, limit):
            raise RuntimeError("API_KEY=do-not-save-this-secret")

        result = await run_research("test", ScriptedModel(), fail, mode="offline_fixture")
        self.assertEqual(result["metrics"]["stop_reason"], "search_error")
        self.assertEqual(result["trace"][0]["error_type"], "RuntimeError")
        self.assertNotIn("do-not-save", json.dumps(result))

    async def test_session_state_does_not_leak_between_questions(self):
        first, _, _ = await self.run_case()
        second, _, _ = await self.run_case("empty")
        self.assertEqual(len(first["sources"]), 2)
        self.assertEqual(second["sources"], [])

    async def test_saved_runs_are_distinct_and_json_reproduces_report(self):
        result, _, _ = await self.run_case()
        with tempfile.TemporaryDirectory() as root:
            a, b = save_run(result, Path(root)), save_run(result, Path(root))
            self.assertNotEqual(a, b)
            saved = json.loads((a / "run.json").read_text(encoding="utf-8"))
            self.assertEqual((a / "report.md").read_text(encoding="utf-8"), render_report(saved))

    async def test_total_deadline_during_search_preserves_partial_evidence(self):
        async def slow(query, limit):
            await asyncio.sleep(5)
            return []

        result = await run_research(
            "test",
            ScriptedModel(),
            slow,
            ResearchConfig(total_timeout=0.04),
            mode="offline_fixture",
        )
        self.assertEqual(result["metrics"]["status"], "task_timeout")
        self.assertLess(result["metrics"]["duration_ms"], 1500)
        self.assertEqual(result["metrics"]["search_attempts"], 1)

    async def test_deadline_is_reported_when_model_adapter_consumes_cancellation(self):
        class SlowModel(ScriptedModel):
            async def _call_api(self, *args, **kwargs):
                await asyncio.sleep(5)
                return await super()._call_api(*args, **kwargs)

        result = await run_research(
            "test",
            SlowModel(),
            FixtureSearch(),
            ResearchConfig(total_timeout=0.04),
            mode="offline_fixture",
        )
        self.assertEqual(result["metrics"]["status"], "task_timeout")
        self.assertEqual(result["metrics"]["stop_reason"], "task_timeout")
        self.assertLess(result["metrics"]["duration_ms"], 1500)

    async def test_iteration_limit_marks_forced_final_answer_incomplete(self):
        result, _, _ = await self.run_case(max_agent_iters=2)
        self.assertEqual(result["metrics"]["status"], "exceed_max_iters")
        self.assertTrue(result["answer"])
        self.assertTrue(any("未完成" in w for w in result["metrics"]["warnings"]))


class ToolPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_calls_cannot_overspend_budget(self):
        calls = []

        async def backend(query, limit):
            calls.append(query)
            await asyncio.sleep(0.001)
            return [Source(query, f"https://example.org/{query}", "data")]

        session = ResearchSession(ResearchConfig(max_searches=2), backend)
        await asyncio.gather(*(session.search_web(f"q{i}") for i in range(8)))
        self.assertEqual(len(calls), 2)
        self.assertEqual(session.attempts, 2)

    async def test_truncation_url_deduplication_and_invalid_source_filter(self):
        async def backend(query, limit):
            return [
                Source("A", "https://example.org/a#one", "x" * 1000),
                Source("B", "https://example.org/a?utm_source=x", "same"),
                Source("Bad", "file:///secret", "private"),
            ]

        session = ResearchSession(ResearchConfig(chars_per_source=100), backend)
        await session.search_web("q")
        self.assertEqual(len(session.sources), 1)
        self.assertEqual(len(next(iter(session.sources.values())).content), 100)

    async def test_blank_query_never_calls_search_provider(self):
        backend = FixtureSearch()
        session = ResearchSession(ResearchConfig(early_stop_duplicates=False), backend)
        await session.search_web(" ")
        self.assertEqual(backend.calls, [])
        self.assertEqual(session.stop_reason, "invalid_query")

    async def test_search_timeout_consumes_one_attempt_and_stops(self):
        async def slow(query, limit):
            await asyncio.sleep(5)

        session = ResearchSession(ResearchConfig(search_timeout=0.01), slow)
        await session.search_web("q")
        await session.search_web("retry")
        self.assertEqual(session.attempts, 1)
        self.assertEqual(session.trace[0]["error_type"], "TimeoutError")


class HTTPContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_adapters_with_mock_http_tool_call_roundtrip(self):
        """Exercise real formatting, SDK deserialization, Agent and HTTP adapters."""
        requests = []

        def search_handler(request):
            self.assertEqual(str(request.url), "https://api.tavily.com/search")
            self.assertEqual(request.headers["Authorization"], "Bearer search-secret")
            body = json.loads(request.content)
            self.assertFalse(body["include_raw_content"])
            self.assertEqual(body["max_results"], 3)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Evidence",
                            "url": "https://example.org/fact",
                            "content": "Fixture fact",
                        }
                    ]
                },
            )

        def model_handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) == 1:
                self.assertIn("search_web", [t["function"]["name"] for t in payload["tools"]])
                msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "tool-1",
                            "type": "function",
                            "function": {
                                "name": "search_web",
                                "arguments": '{"query":"test","knowledge_gap":"verify fact"}',
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                self.assertTrue(any(m["role"] == "tool" for m in payload["messages"]))
                msg = {
                    "role": "assistant",
                    "content": f"Fact [{source_id('https://example.org/fact')}].",
                }
                finish = "stop"
            return httpx.Response(
                200,
                json={
                    "id": f"completion-{len(requests)}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test-chat",
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(model_handler)) as client:
            model = ObservedOpenAIChatModel(
                credential=OpenAICredential(
                    api_key="model-secret", base_url="https://model.invalid/v1"
                ),
                model="test-chat",
                stream=False,
                max_retries=0,
                client_kwargs={"http_client": client, "max_retries": 0},
            )
            result = await run_research(
                "test",
                model,
                TavilySearch("search-secret", httpx.MockTransport(search_handler)),
                ResearchConfig(max_searches=1),
                mode="mock_http",
            )
        self.assertEqual(result["metrics"]["status"], "completed")
        self.assertEqual(result["metrics"]["search_attempts"], 1)
        self.assertEqual(result["metrics"]["token_usage"], {"input": 20, "output": 10})
        self.assertEqual(len(requests), 2)
        self.assertEqual(result["metrics"]["provider_finish_reasons"], ["tool_calls", "stop"])
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
