"""Tavily response validation, policy and simulated MCP propagation without network."""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from agentscope.message import TextBlock
from agentscope.tool import ToolChunk

from as_researcher import mcp_server
from as_researcher.core import ResearchConfig, Source
from as_researcher.mcp_bridge import MCPBackends
from as_researcher.researcher import ResearchSession
from as_researcher.search import SearchBackendError, TavilySearch

VALID = {"title": "Synthetic fact", "url": "https://example.org/fact", "content": "Evidence"}


class TavilyResponseTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, payload=None, *, raw=None, status=200):
        requests = []

        def handler(request):
            requests.append(request)
            if raw is not None:
                return httpx.Response(status, content=raw)
            return httpx.Response(status, json=payload)

        return TavilySearch("offline-placeholder", httpx.MockTransport(handler)), requests

    async def test_invalid_json_and_response_shapes_have_safe_category(self):
        cases = [
            ("invalid_json", None, b"Synthetic malformed provider detail", 1),
            ("null_payload", None, None, 1),
            ("list_payload", [], None, 1),
            ("missing_results", {}, None, 1),
            ("null_results", {"results": None}, None, 1),
            ("object_results", {"results": VALID}, None, 1),
            ("null_entry", {"results": [None]}, None, 1),
            ("string_entry", {"results": ["bad"]}, None, 1),
            ("missing_title", {"results": [{"url": VALID["url"], "content": "Evidence"}]}, None, 1),
            ("missing_content", {"results": [{"title": "Fact", "url": VALID["url"]}]}, None, 1),
            ("title_type", {"results": [dict(VALID, title=42)]}, None, 1),
            ("url_type", {"results": [dict(VALID, url={})]}, None, 1),
            ("content_type", {"results": [dict(VALID, content=[])]}, None, 1),
            ("mixed_partial", {"results": [VALID, dict(VALID, content=None)]}, None, 2),
            ("invalid_beyond_limit", {"results": [VALID, None]}, None, 1),
        ]
        for name, payload, raw, limit in cases:
            with self.subTest(case=name):
                backend, requests = self.backend(payload, raw=raw)
                with self.assertRaises(SearchBackendError) as caught:
                    await backend("official documentation", limit)
                self.assertEqual(caught.exception.search_error_code, "search_invalid_response")
                self.assertEqual(str(caught.exception), "search_invalid_response")
                self.assertEqual(len(requests), 1)
                if raw is not None:
                    self.assertTrue(caught.exception.__suppress_context__)

    async def test_valid_extra_fields_empty_results_and_limit_remain_compatible(self):
        backend, requests = self.backend(
            {"results": [dict(VALID, score=0.8), dict(VALID, title="Second")], "request_id": "x"}
        )
        self.assertEqual(await backend("fact", 1), [Source(**VALID)])
        self.assertEqual(len(requests), 1)
        empty, requests = self.backend({"results": []})
        self.assertEqual(await empty("fact", 1), [])
        self.assertEqual(len(requests), 1)

    async def test_mixed_response_saves_no_partial_evidence_and_stops_after_one_attempt(self):
        backend, requests = self.backend({"results": [VALID, dict(VALID, content=None)]})
        session = ResearchSession(ResearchConfig(max_searches=2), backend)
        first = json.loads((await session.search_web("first query")).content[0].text)
        second = json.loads((await session.search_web("second query")).content[0].text)
        self.assertEqual(first["error_code"], "search_invalid_response")
        self.assertEqual(second["status"], "blocked_after_stop")
        self.assertEqual(session.stop_reason, "search_error")
        self.assertEqual(session.attempts, 1)
        self.assertEqual(session.sources, {})
        self.assertEqual([e["backend_attempted"] for e in session.trace], [True, False])
        self.assertEqual(len(requests), 1)
        self.assertNotIn("Evidence", json.dumps(first) + json.dumps(session.trace))

    async def test_server_and_mock_protocol_bridge_preserve_invalid_response_category(self):
        backend, requests = self.backend({"results": [VALID, None]})
        with (
            patch.dict(os.environ, {"RESEARCH_SEARCH_PROVIDER": "tavily"}, clear=True),
            patch.object(mcp_server, "make_search", return_value=backend),
        ):
            payload = await mcp_server.search("fact", 1)
        self.assertEqual(
            payload,
            {"error": "SearchBackendError", "search_error_code": "search_invalid_response"},
        )
        self.assertEqual(len(requests), 1)
        # Real server function and bridge; the protocol response is mocked, not stdio RPC.
        remote = MCPBackends("fixture", "none")
        remote.search_tool = SimpleNamespace(
            name="mcp__research__search",
            call=AsyncMock(return_value=ToolChunk(content=[TextBlock(text=json.dumps(payload))])),
        )
        session = ResearchSession(ResearchConfig(max_searches=2), remote.search)
        first = json.loads((await session.search_web("first query")).content[0].text)
        await session.search_web("second query")
        self.assertEqual(first["error_code"], "search_invalid_response")
        self.assertEqual(session.sources, {})
        self.assertEqual(session.attempts, 1)
        self.assertEqual([e["backend_attempted"] for e in session.trace], [True, False])
        self.assertEqual(len(remote.calls), 1)
        self.assertEqual(remote.calls[0]["search_error_code"], "search_invalid_response")
        remote.search_tool.call.assert_awaited_once()

    async def test_non_rate_limit_http_error_is_not_reclassified_as_invalid_json(self):
        backend, requests = self.backend(raw=b"Synthetic HTTP failure", status=401)
        with self.assertRaises(httpx.HTTPStatusError):
            await backend("fact", 1)
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
