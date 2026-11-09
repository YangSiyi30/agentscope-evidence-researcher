"""Same-query diagnostic has strict bounds, no default network and safe errors."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from as_researcher import evaluation_terminal as ui
from as_researcher.search import SearchBackendError, TavilySearch
from scripts import compare_search as probe


class ProviderComparisonTests(unittest.TestCase):
    def test_changed_frozen_plan_refused_before_env_or_execution(self):
        plan = probe.make_plan(probe.ROOT / "eval/search_provider_suite.json")
        plan["source_sha256"] = "changed"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf8")
            with patch.object(probe, "execute") as execute, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    probe.main(["--plan", str(path), "--execute"])
            execute.assert_not_called()

    def test_terminal_requires_search_confirmation_and_cancel_only_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            def runner(arguments, folder, **kwargs):
                self.assertEqual(arguments, [])
                (folder / "operation-plan.stdout.txt").write_text(
                    json.dumps(probe.make_plan(probe.ROOT / "eval/search_provider_suite.json")),
                    encoding="utf8",
                )
                return 0

            with (
                patch("builtins.input", side_effect=["8", "", "0"]),
                patch.object(ui, "new_workspace", return_value=workspace),
                patch.object(ui, "run_operation", side_effect=runner) as operation,
                patch("dotenv.load_dotenv", side_effect=AssertionError("no env")),
                redirect_stdout(io.StringIO()),
            ):
                ui.run_evaluation_center()
            self.assertEqual(operation.call_count, 1)
            self.assertEqual(operation.call_args.kwargs["script"], "compare_search.py")

    def test_fixed_runner_refuses_arbitrary_script(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ui.subprocess, "run") as process:
            with self.assertRaises(ValueError):
                ui.run_operation([], Path(tmp), script="../arbitrary.py")
            process.assert_not_called()

    def test_default_plan_has_no_environment_writes_or_network(self):
        with (
            patch("dotenv.load_dotenv", side_effect=AssertionError("no env")),
            patch.object(probe, "make_search", side_effect=AssertionError("no backend")),
            patch.object(Path, "write_text", side_effect=AssertionError("no write")),
            redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(probe.main([]), 0)
        plan = json.loads(out.getvalue())
        self.assertEqual(plan["logical_searches"], 8)
        self.assertEqual(plan["tavily_basic_requests"], 4)
        self.assertEqual(
            [r["provider"] for r in plan["trials"][:4]],
            ["duckduckgo", "tavily", "tavily", "duckduckgo"],
        )

    def test_expanded_query_limits_or_unknown_provider_fail_before_execution(self):
        suite = json.loads((probe.ROOT / "eval/search_provider_suite.json").read_bytes())
        for key, value in [
            ("repeats", 3),
            ("repeats", True),
            ("result_limit", 6),
            ("search_timeout", 21),
            ("providers", ["tavily"]),
            ("queries", []),
        ]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                case = copy.deepcopy(suite)
                case[key] = value
                path = Path(tmp) / "suite.json"
                path.write_text(json.dumps(case), "utf8")
                with self.assertRaises(ValueError):
                    probe.make_plan(path)

    def test_unexecuted_trials_remain_in_planned_denominator(self):
        plan = probe.make_plan(probe.ROOT / "eval/search_provider_suite.json")
        summary = probe.summarize(plan, [])
        self.assertEqual(summary["providers"]["tavily"]["planned"], 4)
        self.assertEqual(summary["providers"]["tavily"]["executed"], 0)
        self.assertIsNone(summary["providers"]["tavily"]["median_ms"])


class ProviderErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_and_quota_failures_are_classified_without_body_or_retry(self):
        for status, code in [
            (401, "search_auth_error"),
            (403, "search_auth_error"),
            (432, "search_quota_exceeded"),
            (433, "search_quota_exceeded"),
        ]:
            with self.subTest(status=status):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(status, content=b"PRIVATE_RESPONSE_AND_KEY")

                backend = TavilySearch("offline-placeholder", httpx.MockTransport(handler))
                with self.assertRaises(SearchBackendError) as caught:
                    await backend("fixed query", 3)
                self.assertEqual(str(caught.exception), code)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
