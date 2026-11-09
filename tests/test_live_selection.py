import contextlib
import io
import json
import unittest
from unittest.mock import patch

from scripts.evaluate_live import batch_exit_code, main, select_cases


class LiveSelectionTests(unittest.TestCase):
    def test_named_case_order_is_preserved_and_default_does_not_change(self):
        cases = [(str(i), "topic", "expected") for i in range(5)]
        self.assertEqual(select_cases(cases), cases[:3])
        self.assertEqual(select_cases(cases, limit=1), cases[:1])
        self.assertEqual(select_cases(cases, ["4", "1"]), [cases[4], cases[1]])

    def test_unknown_duplicates_and_combined_limit_fail_before_execution(self):
        for args in (
            ["--suite", "holdout", "--case", "typo"],
            ["--case", "unanswerable", "--case", "unanswerable"],
            ["--case", "unanswerable", "--limit", "1"],
            ["--chars-per-page", "99"],
            ["--revision-timeout", "61"],
            ["--revision-max-tokens", "4097"],
        ):
            with self.subTest(args=args), patch("scripts.evaluate_live.subprocess.run") as process:
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as error,
                ):
                    main(["--execute", *args])
                self.assertEqual(error.exception.code, 2)
                process.assert_not_called()

    def test_named_unseen_case_plan_has_no_paid_or_filesystem_side_effect(self):
        stdout = io.StringIO()
        with (
            patch("scripts.evaluate_live.subprocess.run") as process,
            patch("pathlib.Path.mkdir") as mkdir,
        ):
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    main(
                        [
                            "--suite",
                            "holdout",
                            "--case",
                            "mcp_tool_errors",
                            "--reader",
                            "browser",
                            "--chars-per-page",
                            "800",
                            "--revise-answer",
                        ]
                    ),
                    0,
                )
            process.assert_not_called()
            mkdir.assert_not_called()
        plan = json.loads(stdout.getvalue()[stdout.getvalue().index("{") :])
        self.assertEqual([case[0] for case in plan["cases"]], ["mcp_tool_errors"])
        self.assertEqual(plan["chars_per_page"], 800)
        self.assertEqual(plan["reader"], "browser")
        self.assertTrue(plan["revise_answer"])
        self.assertEqual(plan["revision_timeout"], 30)

    def test_batch_exit_exposes_warning_failure_and_unexecuted_cases(self):
        good = {"exit_code": 0, "metrics": {"status": "completed"}}
        warning = {"exit_code": 3, "metrics": {"status": "completed"}}
        self.assertEqual(batch_exit_code([good], 1), 0)
        self.assertEqual(batch_exit_code([good, warning], 2), 3)
        self.assertEqual(batch_exit_code([good], 2), 2)
        self.assertEqual(batch_exit_code([{"status": "process_deadline"}], 1), 2)
        self.assertEqual(
            batch_exit_code([{"exit_code": 0, "metrics": {"status": "model_error"}}], 1), 2
        )
