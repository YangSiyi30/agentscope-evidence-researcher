"""Business policy equivalence through real stdio, plus bounded error adaptation."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agentscope.message import TextBlock

from as_researcher.core import ResearchConfig, Source
from as_researcher.execution import execution_summary
from as_researcher.mcp_bridge import MCPBackendError, MCPBackends
from as_researcher.pages import FixtureReader, PageReader, PageReadError
from as_researcher.researcher import ResearchSession
from as_researcher.search import FixtureSearch


def observation(response):
    data = json.loads(response.content[0].text)
    data.pop("fetched_at", None)  # Two runs have independent wall-clock timestamps.
    return data


def counts(session):
    return execution_summary(
        {
            "execution_trace_version": 1,
            "trace": session.trace,
            "page_evidence": list(session.page_evidence.values()),
            "metrics": {
                "tool_invocations": len(session.trace),
                "search_attempts": session.attempts,
                "page_read_attempts": session.page_attempts,
                "pages_read": len(session.page_evidence),
            },
        }
    )


def seed(session, url="https://example.org:8443/"):
    source = Source("synthetic validation source", url, "fixture snippet")
    session.sources[source.url] = source
    return source.id


def mocked_page_tool(payload):
    return SimpleNamespace(
        name="mcp__research__read_public_page",
        call=AsyncMock(return_value=SimpleNamespace(content=[TextBlock(text=json.dumps(payload))])),
    )


class MCPPolicyParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_invalid_id_focus_duplicate_and_independent_budgets_match(self):
        config = ResearchConfig(max_searches=2, max_page_reads=1)
        local = ResearchSession(config, FixtureSearch(), FixtureReader())
        async with MCPBackends("fixture", "fixture") as remote:
            peer = ResearchSession(config, remote.search, remote.read)
            for query in ("memory", "evaluation", "after budget"):
                self.assertEqual(
                    observation(await local.search_web(query)),
                    observation(await peer.search_web(query)),
                )
            first, second = [source.id for source in local.sources.values()]
            for sid, focus, expected in (
                ("SRC-00000000", "", "unknown_source_id"),
                (first, "x" * 121, "focus_too_long"),
                (first, "relevance", None),
                (first, "", "already_attempted"),
                (second, "", "page_budget"),
            ):
                left = observation(await local.read_page(sid, focus))
                right = observation(await peer.read_page(sid, focus))
                self.assertEqual(left, right)
                self.assertEqual(left.get("error"), expected)
            self.assertEqual(local.sources, peer.sources)
            self.assertEqual(local.stop_reason, peer.stop_reason)
            self.assertEqual(counts(local), counts(peer))
            self.assertEqual(
                counts(peer),
                {
                    "counts": {
                        "tool_invocations": 8,
                        "search_attempts": 2,
                        "page_read_attempts": 1,
                        "pages_read": 1,
                        "blocked_requests": 5,
                    },
                    "issues": [],
                },
            )
            self.assertEqual(
                [call["tool"] for call in remote.calls],
                [
                    "mcp__research__search",
                    "mcp__research__search",
                    "mcp__research__read_public_page",
                ],
            )
            self.assertTrue(all(call["status"] == "ok" for call in remote.calls))

    async def test_search_failure_category_stop_and_attempts_match_without_retry(self):
        config = ResearchConfig(max_searches=2)
        local = ResearchSession(config, FixtureSearch("ambiguous_search_error"))
        async with MCPBackends("fixture", "none", scenario="ambiguous_search_error") as remote:
            peer = ResearchSession(config, remote.search)
            for query in ("first", "retry refused"):
                self.assertEqual(
                    observation(await local.search_web(query)),
                    observation(await peer.search_web(query)),
                )
            self.assertEqual(counts(local), counts(peer))
            self.assertEqual(
                counts(peer)["counts"],
                {
                    "tool_invocations": 2,
                    "search_attempts": 1,
                    "page_read_attempts": 0,
                    "pages_read": 0,
                    "blocked_requests": 1,
                },
            )
            self.assertEqual(local.trace[0]["error_code"], peer.trace[0]["error_code"])
            # Backend exception classes can differ; the safe business category is equal.
            self.assertNotEqual(local.trace[0]["error_type"], peer.trace[0]["error_type"])
            self.assertEqual(len(remote.calls), 1)
            self.assertEqual(remote.calls[0]["error_layer"], "backend")

    async def test_page_validation_error_matches_and_preserves_snippet_before_network(self):
        config = ResearchConfig(max_page_reads=1)
        local = ResearchSession(config, FixtureSearch(), PageReader("http"))
        sid = seed(local)
        with patch("socket.getaddrinfo", side_effect=AssertionError("no DNS")):
            local_data = observation(await local.read_page(sid))
        async with MCPBackends("fixture", "http") as remote:
            peer = ResearchSession(config, remote.search, remote.read)
            self.assertEqual(seed(peer), sid)
            peer_data = observation(await peer.read_page(sid))
            self.assertEqual(local_data, peer_data)
            self.assertEqual(peer_data["error"], "port_blocked")
            self.assertEqual(local.sources, peer.sources)
            self.assertEqual(counts(local), counts(peer))
            self.assertEqual(
                counts(peer)["counts"],
                {
                    "tool_invocations": 1,
                    "search_attempts": 0,
                    "page_read_attempts": 1,
                    "pages_read": 0,
                    "blocked_requests": 0,
                },
            )
            self.assertEqual(peer.trace[0]["error_type"], "port_blocked")
            self.assertEqual(len(remote.calls), 1)
            self.assertEqual(remote.calls[0]["error_code"], "port_blocked")
            self.assertEqual(
                observation(await local.read_page(sid)), observation(await peer.read_page(sid))
            )
            self.assertEqual(len(remote.calls), 1)

    async def test_safe_page_codes_preserved_but_arbitrary_backend_errors_filtered(self):
        remote = MCPBackends("fixture", "fixture")
        for code in (
            "redirect_blocked",
            "private_address_blocked",
            "page_too_large",
            "unsupported_content_type",
            "empty_page",
            "excerpt_budget_insufficient",
        ):
            with self.subTest(code=code):
                remote.page_tool = mocked_page_tool({"error": code})
                with self.assertRaisesRegex(PageReadError, code):
                    await remote.read("https://example.org/", 300, "focus")
                self.assertEqual(remote.calls[-1]["error_code"], code)
                remote.page_tool.call.assert_awaited_once_with(
                    url="https://example.org/", max_chars=300, focus="focus"
                )
        for unknown in ("synthetic-untrusted-error-detail", [], {"secret": "synthetic"}, None):
            with self.subTest(unknown=unknown):
                remote.page_tool = mocked_page_tool({"error": unknown})
                with self.assertRaises(MCPBackendError) as exc:
                    await remote.read("https://example.org/", 300)
                self.assertEqual(str(exc.exception), "backend_error")
                self.assertEqual(remote.calls[-1]["error_code"], "backend_error")
        self.assertNotIn("synthetic-untrusted-error-detail", json.dumps(remote.calls))

    async def test_transport_error_stays_transport_and_is_not_automatically_retried(self):
        remote = MCPBackends("fixture", "fixture")
        remote.page_tool = SimpleNamespace(
            name="mcp__research__read_public_page",
            call=AsyncMock(side_effect=RuntimeError("synthetic-untrusted-transport-detail")),
        )
        session = ResearchSession(ResearchConfig(), FixtureSearch(), remote.read)
        sid = seed(session, "https://example.org/")
        data = observation(await session.read_page(sid))
        self.assertEqual(data["error"], "page_transport_error")
        self.assertEqual(session.page_attempts, 1)
        self.assertEqual(len(session.page_evidence), 0)
        self.assertEqual(remote.calls[0]["error_layer"], "transport_or_payload")
        self.assertEqual(remote.calls[0]["error_code"], "RuntimeError")
        remote.page_tool.call.assert_awaited_once()
        self.assertNotIn("synthetic-untrusted-transport-detail", json.dumps([data, remote.calls]))


if __name__ == "__main__":
    unittest.main()
