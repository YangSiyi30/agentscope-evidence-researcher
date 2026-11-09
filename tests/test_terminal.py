"""User journeys through the terminal, without real models or public network."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from as_researcher import cli, terminal


class TerminalTests(unittest.TestCase):
    def drive(self, answers, root):
        stdout = io.StringIO()
        with patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(stdout):
            code = terminal.run_terminal(root)
        return code, stdout.getvalue()

    def test_default_offline_and_mcp_save_answer_and_show_trace(self):
        for transport in ("1", "2"):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "runs"
                with patch("as_researcher.cli.live_model") as live:
                    code, text = self.drive(["", transport, "", "", "", "2", "0", "0"], root)
                    live.assert_not_called()
                paths = list(root.glob("*/run.json"))
                self.assertEqual(len(paths), 1)
                run = json.loads(paths[0].read_text("utf-8"))
                self.assertEqual(code, 0)
                self.assertEqual(run["mode"], "offline_fixture")
                self.assertEqual(run["tool_transport"], "mcp" if transport == "2" else "local")
                self.assertEqual(run["metrics"]["pages_read"], 1)
                self.assertIn(run["answer"], text)
                self.assertIn("read_page", text)
                self.assertIn("固定演示", text)
                self.assertTrue(paths[0].with_name("report.md").exists())
                if transport == "2":
                    self.assertEqual(len(run["mcp"]["calls"]), 2)

    def test_live_requires_explicit_start_and_setup_cancellation_has_no_side_effect(self):
        for answers in (["2", "1", "1", "1", "1", "", "0"], ["1", "q", "0"]):
            with self.subTest(answers=answers), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "absent"
                with (
                    patch.object(terminal, "execute_args") as execute,
                    patch("as_researcher.cli.load_dotenv") as load,
                ):
                    code, _ = self.drive(answers, root)
                    execute.assert_not_called()
                    load.assert_not_called()
                self.assertEqual(code, 0)
                self.assertFalse(root.exists())

    def test_confirmed_live_passes_exact_budgets_to_shared_executor_without_real_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(terminal, "execute_args", side_effect=SystemExit(2)) as execute:
                code, text = self.drive(["2", "2", "2", "1", "1", "LIVE", "0"], root)
            args = execute.call_args.args[0]
            self.assertTrue(args.live)
            self.assertEqual((args.transport, args.reader), ("mcp", "browser"))
            self.assertEqual((args.searches, args.pages, args.max_answer_chars), (0, 1, 700))
            self.assertEqual(args.source_url, [terminal.LIVE_URL])
            self.assertEqual(args.model_retries, 0)
            self.assertFalse(args.revise_answer)
            self.assertEqual(code, 2)
            self.assertIn("不会自动重试", text)

    def test_empty_evidence_is_displayed_as_warning_with_exit_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, text = self.drive(["1", "1", "1", "3", "1", "0", "0"], Path(tmp))
            self.assertEqual(code, 3)
            self.assertIn("证据不足", text)
            self.assertIn("退出码：3", text)

    def test_duplicate_demo_does_not_stop_before_the_duplicate_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, _ = self.drive(["1", "1", "1", "2", "1", "1", "0", "0"], root)
            run = json.loads(next(root.glob("*/run.json")).read_text("utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(run["config"]["max_searches"], 2)
            self.assertEqual(run["metrics"]["search_attempts"], 1)
            self.assertEqual(len(run["trace"]), 2)
            self.assertFalse(run["trace"][1]["backend_attempted"])

    def test_zero_page_budget_uses_search_only_fixture_and_zero_search_uses_given_url(self):
        for searches, pages in (("1", "0"), ("0", "1")):
            with self.subTest(searches=searches, pages=pages), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                code, _ = self.drive(
                    ["1", "1", "2", searches, pages, "120", "700", "1", "1", "0", "0"], root
                )
                run = json.loads(next(root.glob("*/run.json")).read_text("utf-8"))
                self.assertEqual(code, 0)
                self.assertEqual(run["metrics"]["page_read_attempts"], int(pages))
                self.assertEqual(run["metrics"]["search_attempts"], int(searches))

    def test_zero_tool_budget_returns_to_menu_without_creating_a_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(terminal, "execute_args") as execute:
                code, text = self.drive(["1", "1", "2", "0", "0", "120", "700", "0"], Path(tmp))
                execute.assert_not_called()
            self.assertEqual(code, 0)
            self.assertIn("不能同时为0", text)

    def test_numeric_and_source_validation_happen_before_execution(self):
        # live/local/HTTP/custom budget: bad integer, out of range, then valid;
        # custom question starting with '-', then empty/private/valid source.
        answers = [
            "1",
            "1",
            "2",
            "bad",
            "11",
            "0",
            "1",
            "120",
            "700",
            "1800",
            "2",
            "--my-question",
            "",
            "http://127.0.0.1/",
            "https://example.org/guide",
        ]
        with (
            patch("builtins.input", side_effect=answers),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            args = terminal.collect_task(True, Path("reports"))
        parsed = cli.create_parser().parse_args(args)
        self.assertEqual(parsed.topic, "--my-question")
        self.assertEqual(parsed.source_url, ["https://example.org/guide"])
        self.assertEqual(parsed.searches, 0)

    def test_history_is_read_only_nested_and_skips_invalid_json(self):
        original = (
            Path(__file__).resolve().parents[1]
            / "reports/recheck-20261025/live/20261025T080250-90b748a1/run.json"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = root / "nested" / "one" / "run.json"
            saved.parent.mkdir(parents=True)
            saved.write_bytes(original.read_bytes())
            broken = root / "broken" / "run.json"
            broken.parent.mkdir()
            broken.write_text("{broken", "utf-8")
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with (
                patch.object(terminal, "execute_args") as execute,
                patch("as_researcher.cli.load_dotenv") as load,
            ):
                code, text = self.drive(["3", "1", "2", "0", "0"], root)
                execute.assert_not_called()
                load.assert_not_called()
            self.assertEqual(code, 0)
            self.assertIn("model_requests", text)
            self.assertIn("跳过 1", text)
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_open_artifacts_uses_known_paths_and_failure_keeps_menu_available(self):
        original = (
            Path(__file__).resolve().parents[1]
            / "reports/recheck-20261025/live/20261025T080250-90b748a1/run.json"
        )
        run = terminal.load_saved_run(original)
        folder = Path("some run folder")
        with (
            patch("builtins.input", side_effect=["3", "4", "5", "0"]),
            patch.object(terminal, "open_local", side_effect=OSError) as opener,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            terminal.inspect_run(run, folder)
        self.assertEqual(
            [call.args[0] for call in opener.call_args_list],
            [folder / "report.md", folder / "run.json", folder],
        )
        self.assertIn("复制路径", stdout.getvalue())

    def test_ctrl_c_and_eof_exit_without_spinning_or_creating_files(self):
        for error, expected in ((EOFError, 0), (KeyboardInterrupt, 130)):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "absent"
                code, _ = self.drive(error, root)
                self.assertEqual(code, expected)
                self.assertFalse(root.exists())

    def test_terminal_output_strips_screen_and_hyperlink_controls(self):
        value = "safe\x1b[2J\x1b]8;;https://example.org\x07link\x1b]8;;\x07\n中文\x00\x7f"
        self.assertEqual(terminal.terminal_text(value), "safelink\n中文")

    def test_explicit_interactive_rejects_ignored_flags_and_no_args_tty_enters_menu(self):
        with (
            patch.object(terminal, "run_terminal", return_value=0) as wizard,
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(cli.sys.stdout, "isatty", return_value=True),
        ):
            self.assertEqual(cli.main([]), 0)
            wizard.assert_called_once_with(Path("reports"))
        for extra in (["--live"], ["a topic"], ["--searches", "2"]):
            with (
                self.subTest(extra=extra),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                cli.main(["--interactive", *extra])
            self.assertEqual(caught.exception.code, 2)

    def test_noninteractive_keeps_json_output_and_never_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                code = cli.main(
                    [
                        "fixture",
                        "--scenario",
                        "tool_chain",
                        "--reader",
                        "http",
                        "--searches",
                        "1",
                        "--pages",
                        "1",
                        "--strict",
                        "--output-dir",
                        tmp,
                    ]
                )
            data = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(data["pages_read"], 1)
            self.assertTrue((Path(data["output"]) / "run.json").exists())
