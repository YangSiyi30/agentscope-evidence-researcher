"""Sanitized provider failures and real MCP propagation, without live network."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agentscope.message import TextBlock
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from as_researcher.core import ResearchConfig
from as_researcher.mcp_bridge import MCPBackendError, MCPBackends
from as_researcher.researcher import ResearchSession
from as_researcher.search import DDGSearch, SearchBackendError, search_error_code


class SearchDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_errors_are_bounded_categories_and_never_raw_messages(self):
        secret = "credential-must-not-be-persisted"
        for exc, expected in (
            (TimeoutException(secret), "search_timeout"),
            (RatelimitException(secret), "search_rate_limited"),
            (DDGSException("No results found."), "search_no_results_or_unavailable"),
            (DDGSException("https://user:" + secret + "@example.org/"), "search_provider_error"),
        ):
            with self.subTest(expected=expected), patch("ddgs.DDGS") as factory:
                factory.return_value.text.side_effect = exc
                session = ResearchSession(ResearchConfig(max_searches=2), DDGSearch())
                response = await session.search_web("test")
                data = json.loads(response.content[0].text)
                self.assertEqual(data["error_code"], expected)
                self.assertTrue(data["stop"])
                self.assertEqual((data["search_attempts"], data["remaining_searches"]), (1, 1))
                self.assertEqual(session.trace[0]["error_code"], expected)
                self.assertEqual(session.trace[0]["budget_after"], session._budget_observation())
                self.assertNotIn(secret, json.dumps([data, session.trace]))
                await session.search_web("again")
                factory.return_value.text.assert_called_once_with(
                    "test", max_results=3, backend="duckduckgo"
                )
                factory.assert_called_once_with(timeout=10)

    async def test_bad_provider_shapes_fail_without_partial_evidence(self):
        for items in (
            None,
            {},
            ["wrong"],
            [{"title": "title", "href": "https://example.org/", "body": None}],
        ):
            with self.subTest(items=items), patch("ddgs.DDGS") as factory:
                factory.return_value.text.return_value = items
                with self.assertRaises(SearchBackendError) as exc:
                    await DDGSearch()("test", 3)
                self.assertEqual(exc.exception.search_error_code, "search_invalid_response")

    async def test_empty_success_remains_distinct_from_ambiguous_failure(self):
        with patch("ddgs.DDGS") as factory:
            factory.return_value.text.return_value = []
            session = ResearchSession(ResearchConfig(), DDGSearch())
            data = json.loads((await session.search_web("test")).content[0].text)
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["stop_reason"], "no_new_evidence")
            self.assertNotIn("error_code", data)

    async def test_real_mcp_subprocess_preserves_safe_category_and_budget_snapshot(self):
        async with MCPBackends("fixture", "none", scenario="ambiguous_search_error") as remote:
            session = ResearchSession(ResearchConfig(max_searches=2), remote.search)
            data = json.loads((await session.search_web("test")).content[0].text)
            self.assertEqual(data["error_code"], "search_no_results_or_unavailable")
            self.assertEqual(remote.calls[0]["search_error_code"], data["error_code"])
            self.assertEqual(remote.calls[0]["error_layer"], "backend")
            self.assertEqual(session.trace[0]["error_type"], "MCPBackendError")
            self.assertEqual(session.trace[0]["budget_after"]["search_attempts"], 1)
            self.assertEqual(data["sources"], [])
            await session.search_web("retry")
            self.assertEqual(len(remote.calls), 1)

    async def test_unknown_mcp_category_is_not_forwarded_to_trace_or_model(self):
        remote = MCPBackends("fixture", "none")
        for untrusted in ("private-key-like-value", [], {"a": "b"}):
            tool = SimpleNamespace(
                name="mock_search",
                call=AsyncMock(
                    return_value=SimpleNamespace(
                        content=[
                            TextBlock(
                                text=json.dumps(
                                    {"error": "SearchBackendError", "search_error_code": untrusted}
                                )
                            )
                        ]
                    )
                ),
            )
            with self.assertRaises(MCPBackendError) as exc:
                await remote._call(tool)
            self.assertEqual(search_error_code(exc.exception), "search_provider_error")
            self.assertNotIn("search_error_code", remote.calls[-1])
        self.assertNotIn("private-key-like-value", json.dumps(remote.calls))

    async def test_session_deadline_is_timeout_and_cancellation_still_propagates(self):
        async def slow(query, limit):
            await asyncio.Event().wait()

        session = ResearchSession(ResearchConfig(search_timeout=0.01), slow)
        data = json.loads((await session.search_web("test")).content[0].text)
        self.assertEqual(data["error_code"], "search_timeout")
        started = asyncio.Event()

        async def cancellable(query, limit):
            started.set()
            await asyncio.Event().wait()

        session = ResearchSession(ResearchConfig(), cancellable)
        task = asyncio.create_task(session.search_web("test"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(session.trace[0]["status"], "cancelled")
        self.assertEqual(session.trace[0]["budget_after"]["search_attempts"], 1)

    def test_unknown_search_category_cannot_be_constructed(self):
        with self.assertRaises(ValueError):
            SearchBackendError("secret-or-arbitrary-provider-message")
