"""Tests for page policy, MCP transport and model configuration compatibility."""

import asyncio
import json
import os
import socket
import unittest
from unittest.mock import patch

import httpx

from as_researcher.core import ResearchConfig, Source
from as_researcher.mcp_bridge import MCPBackendError, MCPBackends
from as_researcher.models import ScriptedModel, live_model
from as_researcher.pages import (
    FixtureReader,
    PageReadError,
    TextExtractor,
    fetch_html,
    select_excerpt,
    validate_public_url,
)
from as_researcher.researcher import ResearchSession, run_research
from as_researcher.search import FixtureSearch


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_aliases_work_and_explicit_model_values_take_precedence(self):
        env = {
            "LLM_API_KEY": "test-key",
            "LLM_BASE_URL": "https://model.invalid/v1",
            "LLM_MODEL": "alias-model",
        }
        with patch.dict(os.environ, env, clear=True):
            model = live_model()
            self.assertEqual(model.model, "alias-model")
            await model.client.close()
        with patch.dict(os.environ, {**env, "MODEL_NAME": "canonical-model"}, clear=True):
            model = live_model()
            self.assertEqual(model.model, "canonical-model")
            await model.client.close()


class PagePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_source_cannot_trigger_fetch(self):
        calls = []

        async def read(url, limit, **kwargs):
            calls.append(url)
            return "text"

        session = ResearchSession(ResearchConfig(), FixtureSearch(), read)
        response = await session.read_page("https://example.org/private")
        self.assertEqual(json.loads(response.content[0].text)["error"], "unknown_source_id")
        self.assertEqual(calls, [])

    async def test_page_budget_is_independent_from_search_budget(self):
        session = ResearchSession(
            ResearchConfig(max_searches=1, max_page_reads=1), FixtureSearch(), FixtureReader()
        )
        await session.search_web("memory")
        sid = next(iter(session.sources.values())).id
        await session.read_page(sid)
        await session.read_page(sid)
        self.assertEqual(session.stop_reason, "search_budget")
        self.assertEqual(session.page_attempts, 1)
        self.assertEqual(len(session.page_evidence), 1)

    async def test_parallel_page_calls_and_failure_consume_only_budget(self):
        calls = []

        async def read(url, limit, **kwargs):
            calls.append(url)
            await asyncio.sleep(0.001)
            raise PageReadError("redirect_blocked")

        session = ResearchSession(ResearchConfig(max_page_reads=1), FixtureSearch(), read)
        for i in range(4):
            s = Source(str(i), f"https://example.org/{i}", "snippet")
            session.sources[s.url] = s
        await asyncio.gather(*(session.read_page(s.id) for s in session.sources.values()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(session.page_evidence), 0)
        self.assertEqual(session.page_attempts, 1)

    async def test_page_timeout_does_not_lose_search_evidence(self):
        async def slow(url, limit, **kwargs):
            await asyncio.sleep(5)

        session = ResearchSession(ResearchConfig(page_timeout=0.01), FixtureSearch(), slow)
        await session.search_web("memory")
        await session.read_page(next(iter(session.sources.values())).id)
        self.assertEqual(len(session.sources), 1)
        self.assertEqual(session.trace[-1]["error_type"], "page_timeout")

    async def test_private_dns_and_mixed_dns_answers_blocked(self):
        for ips in [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"]]:
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]
            with patch("socket.getaddrinfo", return_value=answers):
                with self.assertRaises(PageReadError):
                    await validate_public_url("https://example.org/")

    async def test_nonstandard_port_and_local_urls_blocked(self):
        for url in ["https://example.org:8443/", "http://169.254.169.254/", "file:///secret"]:
            with self.assertRaises(PageReadError):
                await validate_public_url(url)

    async def test_redirect_type_and_byte_limit(self):
        async def validator(url):
            return url

        responses = [
            httpx.Response(302, headers={"location": "http://localhost/"}),
            httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"pdf"),
            httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 1_000_001),
        ]
        for response in responses:
            with self.assertRaises(PageReadError):
                await fetch_html(
                    "https://example.org/",
                    validator=validator,
                    transport=httpx.MockTransport(lambda request: response),
                )

    async def test_html_parser_removes_script_and_navigation(self):
        parser = TextExtractor()
        parser.feed(
            "<nav>noise</nav><script>secret()</script><main><p>Evidence &amp; facts</p></main>"
        )
        self.assertIn("Evidence & facts", "".join(parser.parts))
        self.assertNotIn("noise", "".join(parser.parts))
        self.assertNotIn("secret", "".join(parser.parts))

    async def test_focused_excerpt_keeps_late_keywords_and_length_bound(self):
        text = (
            "Navigation " * 300
            + "asyncio.timeout cancels the current task. "
            + "More " * 300
            + "wait_for waits."
        )
        excerpt = select_excerpt(text, "asyncio.timeout,wait_for", 1000)
        self.assertIn("asyncio.timeout", excerpt)
        self.assertIn("wait_for", excerpt)
        self.assertIn("[excerpt gap]", excerpt)
        self.assertLessEqual(len(excerpt), 1000)
        self.assertEqual(select_excerpt(text, "missing", 100), text[:100])

    async def test_small_excerpt_keeps_keyword_and_unicode_offsets(self):
        text = "ß" * 400 + "STDIO sends messages. " + "z" * 100
        excerpt = select_excerpt(text, "stdio", 100)
        self.assertIn("STDIO sends messages", excerpt)
        self.assertLessEqual(len(excerpt), 100)

    async def test_inline_html_keeps_technical_identifier_intact(self):
        parser = TextExtractor()
        parser.feed("<p><span>asyncio</span>.<code>timeout</code> cancels tasks.</p>")
        self.assertIn("asyncio.timeout", "".join(parser.parts))


class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_honors_fixture_scenario_and_disabled_reader(self):
        async with MCPBackends("fixture", "none", scenario="empty") as remote:
            self.assertEqual(await remote.search("test", 1), [])
            with self.assertRaisesRegex(MCPBackendError, "reader_disabled"):
                await remote.read("https://example.org/", 100)
            self.assertEqual(remote.calls[-1]["error_code"], "reader_disabled")

    async def test_stdio_tools_are_discovered_and_chain_matches_local_policy(self):
        config = ResearchConfig(max_searches=1)
        local = await run_research(
            "test",
            ScriptedModel("tool_chain"),
            FixtureSearch(),
            config,
            reader=FixtureReader(),
            mode="offline_fixture",
        )
        async with MCPBackends("fixture", "fixture") as remote:
            self.assertEqual(set(remote.discovered_tools), {"search", "read_public_page"})
            result = await run_research(
                "test",
                ScriptedModel("tool_chain"),
                remote.search,
                config,
                reader=remote.read,
                mode="offline_fixture",
            )
            self.assertEqual(
                [c["tool"] for c in remote.calls],
                ["mcp__research__search", "mcp__research__read_public_page"],
            )
        for key in ["status", "search_attempts", "pages_read", "unique_sources", "citations"]:
            self.assertEqual(local["metrics"][key], result["metrics"][key])
        self.assertEqual(local["answer"], result["answer"])

    async def test_server_validates_arguments_and_parent_omits_model_key(self):
        with patch.dict(
            os.environ, {"MODEL_API_KEY": "must-not-forward", "LLM_API_KEY": "must-not-forward"}
        ):
            remote = MCPBackends("fixture", "fixture")
            self.assertNotIn("MODEL_API_KEY", remote.client.mcp_config.env)
            self.assertNotIn("LLM_API_KEY", remote.client.mcp_config.env)
        async with remote:
            with self.assertRaises(MCPBackendError):
                await remote.search("test", 99)


if __name__ == "__main__":
    unittest.main()
