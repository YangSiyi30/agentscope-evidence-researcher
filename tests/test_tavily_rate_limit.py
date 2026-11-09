"""Tavily HTTP 429 classification and tool policy, without network or model calls."""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from agentscope.message import TextBlock
from agentscope.tool import ToolChunk

from as_researcher import mcp_server
from as_researcher.core import ResearchConfig
from as_researcher.mcp_bridge import MCPBackends
from as_researcher.researcher import ResearchSession
from as_researcher.search import SearchBackendError, TavilySearch

PROVIDER_DETAIL = "Synthetic private provider detail must not be saved"


class TavilyRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, *, status=429, body=PROVIDER_DETAIL, retry_after="60"):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(status, text=body, headers={"Retry-After": retry_after})

        return TavilySearch("offline-placeholder", httpx.MockTransport(handler)), requests

    async def test_429_has_safe_category_regardless_of_body_and_retry_after(self):
        for body, retry_after in (
            (json.dumps({"detail": {"error": PROVIDER_DETAIL}}), "60"),
            (PROVIDER_DETAIL, "not-a-delay"),
            ("<html>" + PROVIDER_DETAIL + "</html>", "0"),
            (json.dumps({"results": []}), "60"),
        ):
            with self.subTest(body=body, retry_after=retry_after):
                backend, requests = self.backend(body=body, retry_after=retry_after)
                with self.assertRaises(SearchBackendError) as caught:
                    await backend("official documentation", 1)
                self.assertEqual(caught.exception.search_error_code, "search_rate_limited")
                self.assertEqual(str(caught.exception), "search_rate_limited")
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(len(requests), 1)

    async def test_rate_limit_consumes_one_attempt_and_next_request_is_blocked(self):
        backend, requests = self.backend()
        session = ResearchSession(ResearchConfig(max_searches=2), backend)
        first = json.loads((await session.search_web("first query")).content[0].text)
        second = json.loads((await session.search_web("second query")).content[0].text)
        self.assertEqual(first["status"], "error")
        self.assertEqual(first["error_code"], "search_rate_limited")
        self.assertEqual(first["search_attempts"], 1)
        self.assertEqual(first["remaining_searches"], 1)
        self.assertTrue(first["search_stopped"])
        self.assertEqual(second["status"], "blocked_after_stop")
        self.assertEqual(session.stop_reason, "search_error")
        self.assertEqual(session.attempts, 1)
        self.assertEqual(session.sources, {})
        self.assertEqual([e["backend_attempted"] for e in session.trace], [True, False])
        self.assertEqual(len(requests), 1)
        self.assertNotIn(PROVIDER_DETAIL, json.dumps(first) + json.dumps(session.trace))

    async def test_server_and_mock_protocol_bridge_preserve_rate_limit_category(self):
        backend, requests = self.backend()
        with (
            patch.dict(os.environ, {"RESEARCH_SEARCH_PROVIDER": "tavily"}, clear=True),
            patch.object(mcp_server, "make_search", return_value=backend),
        ):
            payload = await mcp_server.search("official documentation", 1)
        self.assertEqual(
            payload, {"error": "SearchBackendError", "search_error_code": "search_rate_limited"}
        )
        self.assertEqual(len(requests), 1)
        # Real server function and bridge, mocked protocol response; no actual stdio RPC.
        remote = MCPBackends("fixture", "none")
        remote.search_tool = SimpleNamespace(
            name="mcp__research__search",
            call=AsyncMock(return_value=ToolChunk(content=[TextBlock(text=json.dumps(payload))])),
        )
        session = ResearchSession(ResearchConfig(max_searches=2), remote.search)
        first = json.loads((await session.search_web("first query")).content[0].text)
        second = json.loads((await session.search_web("second query")).content[0].text)
        self.assertEqual(first["error_code"], "search_rate_limited")
        self.assertEqual(first["remaining_searches"], 1)
        self.assertEqual(second["status"], "blocked_after_stop")
        self.assertEqual(session.attempts, 1)
        self.assertEqual(session.sources, {})
        self.assertEqual([e["backend_attempted"] for e in session.trace], [True, False])
        self.assertEqual(len(remote.calls), 1)
        self.assertEqual(remote.calls[0]["error_layer"], "backend")
        self.assertEqual(remote.calls[0]["search_error_code"], "search_rate_limited")
        self.assertNotIn(
            PROVIDER_DETAIL, json.dumps(payload) + json.dumps(first) + json.dumps(remote.calls)
        )
        remote.search_tool.call.assert_awaited_once()

    async def test_other_http_errors_keep_their_original_path(self):
        for status in (400, 401, 403, 422, 432, 433, 500, 503):
            with self.subTest(status=status):
                backend, requests = self.backend(status=status)
                with self.assertRaises(httpx.HTTPStatusError) as caught:
                    await backend("official documentation", 1)
                self.assertEqual(caught.exception.response.status_code, status)
                self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
