"""Desktop viewers must not inherit the menu console, stdin or model settings."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from as_researcher import terminal


class ViewerTests(unittest.TestCase):
    def test_environment_keeps_desktop_context_without_model_keys_or_editor_hooks(self):
        with patch.dict(
            os.environ,
            {
                "SystemRoot": "C:/Windows",
                "PATH": "desktop path",
                "USERPROFILE": "profile",
                "MODEL_API_KEY": "model-secret",
                "LLM_API_KEY": "llm-secret",
                "TAVILY_API_KEY": "search-secret",
                "ELECTRON_RUN_AS_NODE": "1",
                "VSCODE_IPC_HOOK_CLI": "editor-hook",
                "PYTHONPATH": "custom-code",
            },
            clear=True,
        ):
            self.assertEqual(
                {k.upper(): v for k, v in terminal.viewer_environment().items()},
                {"SYSTEMROOT": "C:/Windows", "PATH": "desktop path", "USERPROFILE": "profile"},
            )

    @unittest.skipUnless(sys.platform == "win32", "Windows process flags")
    def test_windows_helper_is_detached_with_no_console_streams_or_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run 中文 & $ ' [demo].json"
            path.write_text("{}", "utf-8")
            with patch.object(terminal.subprocess, "run") as execute:
                terminal.open_local(path)
            command = execute.call_args.args[0]
            options = execute.call_args.kwargs
            self.assertEqual(
                command[:4], [str(Path(sys.executable).with_name("pythonw.exe")), "-I", "-S", "-c"]
            )
            self.assertEqual(command[-1], str(path.resolve()))
            self.assertNotIn(str(path), command[-2])
            self.assertEqual(
                options["creationflags"],
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            for stream in ("stdin", "stdout", "stderr"):
                self.assertEqual(options[stream], subprocess.DEVNULL)
            self.assertFalse(options["shell"])
            self.assertTrue(options["close_fds"])
            self.assertTrue(options["check"])
            self.assertEqual(options["timeout"], 10)
            self.assertEqual(options["cwd"], path.parent.resolve())

    def test_posix_openers_are_quiet_and_in_a_separate_session(self):
        for platform, opener in (("darwin", "open"), ("linux", "xdg-open")):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                with (
                    patch.object(terminal.sys, "platform", platform),
                    patch.object(terminal.subprocess, "run") as execute,
                ):
                    terminal.open_local(folder)
                self.assertEqual(execute.call_args.args[0], [opener, str(folder.resolve())])
                options = execute.call_args.kwargs
                self.assertTrue(options["start_new_session"])
                self.assertNotIn("creationflags", options)
                self.assertEqual(options["stderr"], subprocess.DEVNULL)
                self.assertEqual(options["stdin"], subprocess.DEVNULL)
                self.assertEqual(options["stdout"], subprocess.DEVNULL)

    def test_missing_path_fails_before_any_external_process(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(terminal.subprocess, "run") as execute,
        ):
            with self.assertRaises(FileNotFoundError):
                terminal.open_local(Path(tmp) / "missing.json")
            execute.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Real Windows detached console probe")
    def test_actual_noisy_helper_has_no_console_and_cannot_consume_menu_input(self):
        # Substitute only the desktop action. Keep the production process flags,
        # streams, timeout, environment and cwd, and run an actual subprocess.
        real_run = subprocess.run
        probe = """import ctypes,json,os,sys
from pathlib import Path
info={'has_console': bool(ctypes.windll.kernel32.GetConsoleWindow()),
      'stdin_at_eof': sys.stdin.read(1) == '',
      'received_model_key': bool(os.getenv('MODEL_API_KEY'))}
print('NOISY_VIEWER_STDOUT',flush=True)
print('NOISY_VIEWER_STDERR',file=sys.stderr,flush=True)
Path(sys.argv[1]).write_text(json.dumps(info),encoding='utf-8')
"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            artifact = folder / "run.json"
            artifact.write_text("{}", "utf-8")
            output = folder / "probe.json"

            def noisy_action(command, **options):
                return real_run([command[0], "-I", "-S", "-c", probe, str(output)], **options)

            with (
                patch.object(terminal.subprocess, "run", side_effect=noisy_action),
                patch.dict(os.environ, {"MODEL_API_KEY": "test-only-secret"}),
            ):
                terminal.open_local(artifact)
            self.assertEqual(
                json.loads(output.read_text("utf-8")),
                {"has_console": False, "stdin_at_eof": True, "received_model_key": False},
            )

    def sample(self):
        return {
            "mode": "offline_fixture",
            "topic": "saved question",
            "answer": "saved answer",
            "metrics": {
                "status": "completed",
                "stop_reason": "agent_completed",
                "warnings": [],
                "unique_sources": 1,
            },
            "trace": [],
            "sources": [],
        }

    def test_terminal_run_json_option_is_read_only_without_an_editor_or_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            saved = folder / "run.json"
            saved.write_text(json.dumps(self.sample(), ensure_ascii=False), "utf-8")
            before = saved.read_bytes()
            with (
                patch("builtins.input", side_effect=["6", "0"]),
                patch.object(terminal, "open_local") as opener,
                patch.object(terminal, "execute_args") as execute,
                patch("as_researcher.cli.load_dotenv") as load,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                terminal.inspect_run(self.sample(), folder)
            self.assertIn('"answer": "saved answer"', output.getvalue())
            self.assertIn("只读", output.getvalue())
            self.assertEqual(saved.read_bytes(), before)
            opener.assert_not_called()
            execute.assert_not_called()
            load.assert_not_called()

    def test_unreadable_or_corrupt_run_json_does_not_break_the_menu(self):
        for content in (None, "{bad json"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                if content is not None:
                    (folder / "run.json").write_text(content, "utf-8")
                with (
                    patch("builtins.input", side_effect=["6", "1", "0"]),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    terminal.inspect_run(self.sample(), folder)
                self.assertIn("无法读取有效的运行记录", output.getvalue())
                self.assertIn("saved answer", output.getvalue())

    def test_timeout_and_nonzero_helper_exit_return_to_menu_without_retry(self):
        for error in (
            subprocess.TimeoutExpired(["viewer"], 10),
            subprocess.CalledProcessError(1, ["viewer"]),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch("builtins.input", side_effect=["4", "0"]),
                patch.object(terminal, "open_local", side_effect=error) as opener,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                terminal.inspect_run(self.sample(), Path("saved folder"))
            opener.assert_called_once()
            self.assertIn("不会自动重试", output.getvalue())
            self.assertIn("复制路径", output.getvalue())

    def test_success_reports_submission_without_waiting_for_editor_to_close(self):
        with (
            patch("builtins.input", side_effect=["3", "4", "5", "0"]),
            patch.object(terminal, "open_local") as opener,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            terminal.inspect_run(self.sample(), Path("saved folder"))
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(output.getvalue().count("已提交默认程序打开请求"), 3)
