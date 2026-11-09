"""Fixed offline commands, safe summaries and honest fail-fast results."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import verify_offline as verify


class OfflineDeliveryTests(unittest.TestCase):
    def record(self, folder):
        record = verify.plan()
        record.update(output=str(folder / "summary.json"), mcp_output=str(folder / "mcp"))
        return record

    def test_default_only_plans_without_environment_clients_or_writes(self):
        output = io.StringIO()
        with (
            patch("dotenv.load_dotenv", side_effect=AssertionError("no dotenv")),
            patch("as_researcher.models.live_model", side_effect=AssertionError("no model")),
            patch.object(verify.subprocess, "Popen", side_effect=AssertionError("no child")),
            patch.object(Path, "mkdir", side_effect=AssertionError("no directory")),
            patch.object(Path, "write_text", side_effect=AssertionError("no write")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(verify.main([]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "planned")
        self.assertIsNone(result["passed"])
        self.assertTrue(all(step["status"] == "not_run" for step in result["steps"]))
        self.assertEqual(len(result["required_historical_files"]), 8)

    def test_no_paid_flags_and_timeout_expansion(self):
        for args in (["--live"], ["--execute"], ["--step-timeout", "0"], ["--step-timeout", "61"]):
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                verify.main(args)
            self.assertEqual(error.exception.code, 2)

    def test_environment_excludes_model_keys_provider_controls_and_pythonpath(self):
        with patch.dict(
            verify.os.environ,
            {
                "PATH": "safe path",
                "MODEL_API_KEY": "SECRET",
                "LLM_API_KEY": "SECRET",
                "TAVILY_API_KEY": "SECRET",
                "PYTHONPATH": "unsafe",
                "RESEARCH_SEARCH_PROVIDER": "tavily",
            },
            clear=True,
        ):
            env = verify.child_environment()
        self.assertEqual(env["PATH"], "safe path")
        self.assertNotIn("SECRET", str(env))
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("RESEARCH_SEARCH_PROVIDER", env)
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_commands_are_fixed_unknown_steps_rejected_and_cwd_not_from_plan(self):
        for step in verify.STEPS:
            args = verify.command(step, Path("reports/mcp"))
            self.assertEqual(args[0], verify.sys.executable)
            self.assertNotIn("--live", args)
            self.assertNotIn("--execute", args)
        with self.assertRaisesRegex(ValueError, "unknown_offline_step"):
            verify.command("live", Path("reports/mcp"))
        process = Mock()
        process.wait.return_value = 0
        with patch.object(verify.subprocess, "Popen", return_value=process) as popen:
            code, stdout = verify.run_command(["unused-test-command"], timeout=1)
        self.assertEqual((code, stdout), (0, ""))
        self.assertEqual(popen.call_args.kwargs["cwd"], verify.ROOT)
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_success_requires_valid_results_from_every_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.record(Path(tmp))
            runner = Mock(return_value=(0, "ignored"))
            with (
                patch.object(
                    verify, "checked_output", side_effect=lambda step, _: {"checked": step}
                ),
                patch.object(verify, "capture_artifacts"),
            ):
                self.assertEqual(verify.execute(record, runner=runner), 0)
            saved = json.loads(Path(record["output"]).read_bytes())
            self.assertTrue(saved["passed"])
            self.assertEqual(runner.call_count, 4)
            self.assertTrue(all(step["status"] == "passed" for step in saved["steps"]))

    def test_failed_child_stops_and_does_not_save_raw_stdout_or_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.record(Path(tmp))
            runner = Mock(return_value=(7, "API_KEY=SECRET private traceback"))
            self.assertEqual(verify.execute(record, runner=runner), 1)
            body = Path(record["output"]).read_text("utf-8")
            self.assertNotIn("SECRET", body)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(record["steps"][0]["returncode"], 7)
            self.assertTrue(
                all(
                    step["status"] == "not_run" and step["returncode"] is None
                    for step in record["steps"][1:]
                )
            )

    def test_timeout_is_saved_and_only_direct_child_is_killed(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("cmd", 1), 0]
        with (
            patch.object(verify.subprocess, "Popen", return_value=process),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            verify.run_command(["test"], timeout=1)
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_args_list[-1].kwargs, {"timeout": 5})
        with tempfile.TemporaryDirectory() as tmp:
            record = self.record(Path(tmp))
            runner = Mock(side_effect=subprocess.TimeoutExpired("SECRET", 1))
            self.assertEqual(verify.execute(record, runner=runner), 1)
            self.assertEqual(record["steps"][0]["status"], "timeout")
            self.assertIsNone(record["steps"][0]["returncode"])
            self.assertNotIn("SECRET", Path(record["output"]).read_text("utf-8"))

    def test_zero_exit_with_invalid_result_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.record(Path(tmp))
            record["steps"] = record["steps"][1:]
            runner = Mock(return_value=(0, "not JSON SECRET"))
            self.assertEqual(verify.execute(record, runner=runner), 1)
            self.assertEqual(record["steps"][0]["status"], "error")
            self.assertEqual(runner.call_count, 1)
            self.assertNotIn("SECRET", Path(record["output"]).read_text("utf-8"))

    def test_missing_history_saves_recovery_without_running_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.record(Path(tmp))
            record["missing_historical_files"] = ["reports/old/run.json"]
            runner = Mock(side_effect=AssertionError("must not run"))
            self.assertEqual(verify.execute(record, runner=runner), 2)
            self.assertEqual(record["status"], "missing_historical_artifacts")
            self.assertTrue(Path(record["output"]).is_file())
            runner.assert_not_called()

    def test_existing_summary_refused_before_any_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.record(Path(tmp))
            target = Path(record["output"])
            target.write_bytes(b"ORIGINAL")
            runner = Mock()
            with self.assertRaises(FileExistsError):
                verify.execute(record, runner=runner)
            self.assertEqual(target.read_bytes(), b"ORIGINAL")
            runner.assert_not_called()

    def test_output_and_history_escape_paths_rejected(self):
        with self.assertRaisesRegex(ValueError, "output_must_be_new_reports_json"):
            verify.plan(verify.ROOT / "outside.json")
        with self.assertRaisesRegex(ValueError, "output_must_be_new_reports_json"):
            verify.plan(verify.ROOT / "reports/test.txt")
        original_loads = verify.json.loads

        def changed_fixture(raw):
            data = original_loads(raw)
            if data.get("kind") == "offline_local_edit_contract_development":
                data["source_binding"]["edit"]["file"] = "../outside.json"
            return data

        with (
            patch.object(verify.json, "loads", side_effect=changed_fixture),
            self.assertRaisesRegex(ValueError, "invalid_historical_path"),
        ):
            verify.historical_files()

    def test_saved_command_text_is_not_executed_and_exceptions_keep_type_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.record(Path(tmp))
            record["steps"][0]["command"] = ["dangerous", "--live"]
            runner = Mock(side_effect=OSError("SECRET"))
            self.assertEqual(verify.execute(record, runner=runner), 1)
            self.assertEqual(
                runner.call_args.args[0], verify.command("local_replay", Path(record["mcp_output"]))
            )
            self.assertEqual(record["steps"][0]["error_type"], "OSError")
            self.assertNotIn("SECRET", Path(record["output"]).read_text("utf-8"))

    def test_keyboard_interrupt_saves_incomplete_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.record(Path(tmp))
            self.assertEqual(
                verify.execute(record, runner=Mock(side_effect=KeyboardInterrupt)), 130
            )
            self.assertEqual(record["status"], "interrupted")
            self.assertEqual(record["steps"][0]["status"], "interrupted")
            self.assertTrue(all(step["status"] == "not_run" for step in record["steps"][1:]))

    def test_step_summaries_reject_false_success_and_filter_extra_payload(self):
        for step, data in (
            ("mcp_fixture", {"passed": False}),
            ("saved_edit", {"kind": "other"}),
            ("delivery_audit", {"passed": False}),
        ):
            with self.subTest(step=step), self.assertRaises(ValueError):
                verify.checked_output(step, json.dumps(data))
        data = {
            "passed": True,
            "output": "reports/audit.json",
            "files_scanned": 1,
            "local_links_checked": 2,
            "reports_checked": 3,
            "historical_reports_not_reproduced": 0,
            "secret": "SECRET",
        }
        self.assertNotIn("SECRET", str(verify.checked_output("delivery_audit", json.dumps(data))))


if __name__ == "__main__":
    unittest.main()
