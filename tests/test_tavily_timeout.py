"""Tavily timeout categories and policy propagation, with no network or model."""

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


class TavilyTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, exception_type=httpx.ReadTimeout):
        requests = []

        def handler(request):
            requests.append(request)
            raise exception_type("Synthetic provider detail must not be saved", request=request)

        return TavilySearch("offline-placeholder", transport=httpx.MockTransport(handler)), requests

    async def test_all_httpx_timeout_phases_have_one_safe_category_and_no_retry(self):
        for exception_type in (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        ):
            with self.subTest(phase=exception_type.__name__):
                backend, requests = self.backend(exception_type)
                with self.assertRaises(SearchBackendError) as caught:
                    await backend("official documentation", 1)
                self.assertEqual(caught.exception.search_error_code, "search_timeout")
                self.assertEqual(str(caught.exception), "search_timeout")
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(len(requests), 1)

    async def test_timeout_consumes_one_attempt_and_next_request_is_blocked(self):
        backend, requests = self.backend()
        session = ResearchSession(ResearchConfig(max_searches=2), backend)
        first = json.loads((await session.search_web("first query")).content[0].text)
        second = json.loads((await session.search_web("second query")).content[0].text)
        self.assertEqual(first["error_code"], "search_timeout")
        self.assertEqual(second["status"], "blocked_after_stop")
        self.assertEqual(session.stop_reason, "search_error")
        self.assertEqual(session.attempts, 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(session.trace), 2)
        self.assertEqual([e["backend_attempted"] for e in session.trace], [True, False])
        self.assertEqual(session.sources, {})
        self.assertNotIn("Synthetic provider detail", json.dumps(first) + json.dumps(session.trace))

    async def test_server_and_mock_protocol_bridge_preserve_timeout_category(self):
        backend, requests = self.backend()
        with (
            patch.dict(os.environ, {"RESEARCH_SEARCH_PROVIDER": "tavily"}, clear=True),
            patch.object(mcp_server, "make_search", return_value=backend),
        ):
            payload = await mcp_server.search("official documentation", 1)
        self.assertEqual(
            payload, {"error": "SearchBackendError", "search_error_code": "search_timeout"}
        )
        self.assertEqual(len(requests), 1)

        # Exercise the real bridge/policy with a mocked protocol response.
        # No MCP connection or stdio RPC is performed in this test.
        remote = MCPBackends("fixture", "none")
        remote.search_tool = SimpleNamespace(
            name="mcp__research__search",
            call=AsyncMock(return_value=ToolChunk(content=[TextBlock(text=json.dumps(payload))])),
        )
        session = ResearchSession(ResearchConfig(max_searches=2), remote.search)
        first = json.loads((await session.search_web("first query")).content[0].text)
        await session.search_web("second query")
        self.assertEqual(first["error_code"], "search_timeout")
        self.assertEqual(session.attempts, 1)
        self.assertEqual([e["backend_attempted"] for e in session.trace], [True, False])
        self.assertEqual(len(remote.calls), 1)
        self.assertEqual(remote.calls[0]["search_error_code"], "search_timeout")
        remote.search_tool.call.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
