"""Reports use recorded tool state, preserve old outputs, and never rewrite answers."""

import asyncio
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from as_researcher.cli import main as cli_main
from as_researcher.core import ResearchConfig, Source
from as_researcher.execution import execution_summary
from as_researcher.models import ScriptedModel
from as_researcher.pages import FixtureReader
from as_researcher.researcher import ResearchSession, render_report, run_research
from as_researcher.search import FixtureSearch
from scripts.export_execution_report import export

ROOT = Path(__file__).resolve().parents[1]


def state_record(session):
    return {
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


class ExecutionSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_loop_saves_versioned_report_without_changing_answer(self):
        run = await run_research(
            "fixture",
            ScriptedModel("tool_chain"),
            FixtureSearch(),
            ResearchConfig(max_searches=1),
            reader=FixtureReader(),
            mode="offline_fixture",
        )
        before = json.dumps(run, sort_keys=True)
        summary = execution_summary(run)
        self.assertEqual(
            summary["counts"],
            {
                "tool_invocations": 2,
                "search_attempts": 1,
                "page_read_attempts": 1,
                "pages_read": 1,
                "blocked_requests": 0,
            },
        )
        self.assertEqual(run["report_renderer_version"], 6)
        self.assertEqual(summary["issues"], [])
        report = render_report(run)
        self.assertIn("| 后端搜索尝试 | 1 |", report)
        self.assertIn(run["answer"], report)
        self.assertEqual(before, json.dumps(run, sort_keys=True))

    async def test_failed_search_then_blocked_request_count_one_backend(self):
        backend = FixtureSearch("ambiguous_search_error")
        session = ResearchSession(ResearchConfig(max_searches=1), backend)
        await session.search_web("first")
        await session.search_web("second")
        summary = execution_summary(state_record(session))
        self.assertEqual(backend.calls, ["first"])
        self.assertEqual(summary["counts"]["tool_invocations"], 2)
        self.assertEqual(summary["counts"]["search_attempts"], 1)
        self.assertEqual(summary["counts"]["blocked_requests"], 1)
        self.assertEqual([e["backend_attempted"] for e in session.trace], [True, False])

    async def test_duplicate_and_invalid_page_requests_are_blocked_not_attempted(self):
        session = ResearchSession(ResearchConfig(max_searches=3), FixtureSearch(), FixtureReader())
        await session.search_web("memory")
        await session.search_web("memory")
        await session.read_page("unknown")
        sid = next(iter(session.sources.values())).id
        await session.read_page(sid)
        await session.read_page(sid)
        counts = execution_summary(state_record(session))["counts"]
        self.assertEqual(
            counts,
            {
                "tool_invocations": 5,
                "search_attempts": 1,
                "page_read_attempts": 1,
                "pages_read": 1,
                "blocked_requests": 3,
            },
        )

    async def test_cancelled_backend_attempt_is_not_classified_as_blocked(self):
        started = asyncio.Event()

        async def reader(url, limit, **kwargs):
            started.set()
            await asyncio.Event().wait()

        session = ResearchSession(ResearchConfig(), FixtureSearch(), reader)
        source = Source("test", "https://example.org/", "fixture")
        session.sources[source.url] = source
        task = asyncio.create_task(session.read_page(source.id))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        counts = execution_summary(state_record(session))["counts"]
        self.assertEqual(
            (counts["page_read_attempts"], counts["blocked_requests"], counts["pages_read"]),
            (1, 0, 0),
        )
        self.assertEqual(session.trace[0]["status"], "cancelled")

    def test_saved_legacy_report_is_byte_equivalent_and_export_is_separate(self):
        path = ROOT / "reports/live-v013/20260929T113813-d4cc0e62/run.json"
        original = path.read_bytes()
        run = json.loads(original)
        self.assertEqual(render_report(run), path.with_name("report.md").read_text("utf-8"))
        self.assertIsNone(execution_summary(run)["counts"]["blocked_requests"])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "separate.md"
            export(path, output)
            body = output.read_text("utf-8")
            self.assertIn(run["answer"], body)
            self.assertIn("| 后端搜索尝试 | 1 |", body)
            self.assertIn("| 被拦截、未进入后端的请求 | 未记录或无法核对 |", body)
            with self.assertRaises(FileExistsError):
                export(path, output)
        self.assertEqual(path.read_bytes(), original)

    def test_missing_invalid_and_conflicting_counts_are_not_guessed(self):
        run = {
            "execution_trace_version": 1,
            "trace": [{"tool": "search_web", "backend_attempted": True}],
            "page_evidence": [],
            "metrics": {"tool_invocations": 1, "search_attempts": 9, "pages_read": 1},
        }
        summary = execution_summary(run)
        self.assertIsNone(summary["counts"]["search_attempts"])
        self.assertIsNone(summary["counts"]["page_read_attempts"])
        self.assertIsNone(summary["counts"]["pages_read"])
        self.assertTrue(summary["issues"])
        run["metrics"]["tool_invocations"] = 2
        self.assertIsNone(execution_summary(run)["counts"]["tool_invocations"])
        self.assertIsNone(execution_summary(run)["counts"]["blocked_requests"])
        for value in (True, -1, "2", None):
            self.assertIsNone(
                execution_summary({"metrics": {"search_attempts": value}})["counts"][
                    "search_attempts"
                ]
            )
        old = copy.deepcopy(run)
        old.pop("execution_trace_version")
        self.assertIsNone(execution_summary(old)["counts"]["blocked_requests"])

    def test_unsupported_report_version_rejected(self):
        for version in (True, 99, "2"):
            with self.assertRaises(ValueError):
                render_report({"report_renderer_version": version})

    def test_saved_v2_real_success_and_failure_reports_remain_identical(self):
        for relative in (
            "reports/synthetic-omission-20260929T122244-c0d9dd/requested_example_omitted/20260929T122250-aa9e450d/run.json",
            "reports/live-v015/20260929T122408-8da26d47/run.json",
            "reports/live-v015/20260929T122433-841f70cb/run.json",
        ):
            path = ROOT / relative
            run = json.loads(path.read_bytes())
            self.assertEqual(run["report_renderer_version"], 2)
            self.assertEqual(render_report(run), path.with_name("report.md").read_text("utf-8"))

    def test_v3_failure_placeholder_is_not_labeled_model_original(self):
        path = ROOT / "reports/live-v015/20260929T122408-8da26d47/run.json"
        run = json.loads(path.read_bytes())
        original = copy.deepcopy(run)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "failure.md"
            export(path, output)
            body = output.read_text("utf-8")
            self.assertIn("## 保存的回答", body)
            self.assertNotIn("## 模型回答（原文）", body)
            self.assertIn(run["answer"], body)
            self.assertIn("程序提示", body)
        self.assertEqual(json.loads(path.read_bytes()), original)

    async def test_v3_revision_candidate_is_saved_answer_not_labeled_original(self):
        run = await run_research(
            "fixture",
            ScriptedModel("tool_chain"),
            FixtureSearch(),
            ResearchConfig(max_searches=1),
            reader=FixtureReader(),
            mode="offline_fixture",
        )
        original = run["answer"]
        run.update(
            answer="保存的合成候选",
            revision={
                "applied": True,
                "original_answer": original,
                "candidate_answer": "保存的合成候选",
            },
        )
        before = copy.deepcopy(run)
        body = render_report(run)
        self.assertIn("## 保存的回答", body)
        self.assertIn("保存的合成候选", body)
        self.assertNotIn("## 模型回答（原文）", body)
        self.assertEqual(run, before)

    def test_cli_search_error_precedes_strict_warning_exit_code(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "sys.argv",
                ["as-research", "--scenario", "search_error", "--strict", "--output-dir", tmp],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli_main(), 2)
