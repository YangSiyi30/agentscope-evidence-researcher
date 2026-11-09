"""Supplied URL locations enable bounded reads without inventing retrieval evidence."""

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from as_researcher.cli import main
from as_researcher.core import ResearchConfig, Source
from as_researcher.evidence import evidence_diagnostics
from as_researcher.mcp_bridge import MCPBackends
from as_researcher.pages import FixtureReader, PageReadError
from as_researcher.researcher import ResearchSession, render_report
from as_researcher.source_inputs import normalize_source_urls
from scripts.observe_source_urls import URL, inspect_source_flow, observe_source_flow


class SourceInputTests(unittest.TestCase):
    def test_offline_cli_supplied_source_uses_normal_entry_and_persists_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            args = [
                "as-research",
                "读取指定来源",
                "--source-url",
                URL,
                "--searches",
                "0",
                "--reader",
                "http",
                "--pages",
                "1",
                "--output-dir",
                directory,
                "--strict",
            ]
            with (
                patch.object(sys, "argv", args),
                patch("as_researcher.cli.load_dotenv"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)
            path = next(Path(directory).glob("*/run.json"))
            run = json.loads(path.read_bytes())
            self.assertEqual(run["provided_source_urls"], [URL])
            self.assertEqual(run["metrics"]["search_attempts"], 0)
            self.assertEqual(run["metrics"]["pages_read"], 1)
            self.assertEqual(run["metrics"]["fixture_model_calls"], 2)
            self.assertEqual(run["reader"], "fixture")

    def test_normalization_deduplication_and_invalid_inputs_are_bounded(self):
        self.assertEqual(normalize_source_urls([URL + "#section", URL]), (URL,))
        for value in [
            None,
            URL,
            [123],
            [""],
            ["https://example.org/" + "x" * 2048],
            ["http://127.0.0.1/"],
            ["https://example.org:8443/"],
            ["https://user:pass@example.org/"],
            [URL] * 4,
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_source_urls(value)

    def test_cli_rejects_source_mode_misconfiguration_before_config_or_model_loading(self):
        for args in [
            ["--source-url", URL],
            ["--source-url", URL, "--reader", "http", "--pages", "0"],
            ["--source-url", "http://127.0.0.1/", "--reader", "http"],
        ]:
            with (
                self.subTest(args=args),
                patch.object(sys, "argv", ["as-research", "--live", *args]),
                patch("as_researcher.cli.load_dotenv") as load,
                patch("as_researcher.cli.live_model") as model,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as caught:
                    main()
                self.assertEqual(caught.exception.code, 2)
                load.assert_not_called()
                model.assert_not_called()


class SourceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_catalog_and_page_observation_without_fake_snippet_or_trace(self):
        run, observation = await observe_source_flow()
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(run["report_renderer_version"], 6)
        self.assertIn("用户提供URL（非搜索摘要）", render_report(run))
        self.assertNotIn("检索到的来源", render_report(run))
        sid = run["sources"][0]["id"]
        self.assertFalse(
            evidence_diagnostics(f"[{sid}@snippet]", run["sources"], run["page_evidence"])[
                "contract_valid"
            ]
        )

    async def test_reader_failure_does_not_turn_known_url_into_evidence(self):
        async def fail(*args, **kwargs):
            raise PageReadError("empty_page")

        run, _ = await observe_source_flow(reader=fail)
        self.assertEqual(
            (
                run["metrics"]["search_attempts"],
                run["metrics"]["page_read_attempts"],
                run["metrics"]["pages_read"],
            ),
            (0, 1, 0),
        )
        self.assertEqual(run["sources"][0]["content"], "")
        self.assertFalse(run["metrics"]["evidence_levels"]["contract_valid"])
        self.assertTrue(any("未取得外部证据" in w for w in run["metrics"]["warnings"]))

    async def test_zero_search_budget_removes_search_tool_and_blocks_direct_calls(self):
        async def forbidden(*args, **kwargs):
            self.fail("Search backend must not run")

        session = ResearchSession(
            ResearchConfig(max_searches=0), forbidden, FixtureReader(), source_urls=[URL]
        )
        schemas = await session.toolkit().get_tool_schemas()
        self.assertEqual([s["function"]["name"] for s in schemas], ["read_page"])
        data = json.loads((await session.search_web("ignored")).content[0].text)
        self.assertTrue(data["stop"])
        self.assertEqual(session.attempts, 0)
        blocked = json.loads((await session.read_page("SRC-00000000")).content[0].text)
        self.assertEqual(blocked["error"], "unknown_source_id")
        self.assertEqual(session.page_attempts, 0)

    async def test_actual_search_upgrades_url_location_then_keeps_first_snippet(self):
        values = ["first retrieved snippet", "later changed snippet"]

        async def search(*args, **kwargs):
            return [Source("retrieved title", URL, values.pop(0))]

        session = ResearchSession(
            ResearchConfig(max_searches=2), search, FixtureReader(), source_urls=[URL]
        )
        sid = session.sources[URL].id
        first = json.loads((await session.search_web("first")).content[0].text)
        self.assertEqual(first["sources"][0]["id"], sid)
        self.assertEqual(first["sources"][0]["content"], "first retrieved snippet")
        await session.search_web("second")
        self.assertEqual(session.sources[URL].content, "first retrieved snippet")

    async def test_provided_url_reads_through_real_stdio_without_search_rpc(self):
        async with MCPBackends("fixture", "fixture") as remote:
            run, observation = await observe_source_flow(reader=remote.read)
            self.assertTrue(observation["passed"], observation["checks"])
            self.assertEqual(len(remote.calls), 1)
            self.assertEqual(remote.calls[0]["tool"], "mcp__research__read_public_page")
            self.assertEqual(run["metrics"]["search_attempts"], 0)

    async def test_checker_rejects_missing_first_catalog_despite_valid_page_and_trace(self):
        run, observation = await observe_source_flow()
        turns = copy.deepcopy(observation["model_turns"])
        turns[0]["registered_sources_received"] = []
        result = inspect_source_flow(run, turns)
        self.assertFalse(result["passed"])
        self.assertEqual(run["metrics"]["pages_read"], 1)
        self.assertEqual(run["trace"][0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
