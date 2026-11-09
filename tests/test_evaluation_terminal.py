"""Guided user journeys; labels and subprocess results are synthetic fixtures."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from as_researcher import evaluation_terminal as ui
from as_researcher import terminal
from as_researcher.benchmark import (
    assess_saved_batch,
    digest,
    load_bound_batch,
    load_suite,
    prepare_task_review,
    source_digest,
)
from as_researcher.review import fingerprint
from tests.test_benchmark import ROOT, example_run, task


def bound_fixture(batch):
    source = ROOT / "eval/research_suite.json"
    run = example_run()
    run.update(
        benchmark={"trial_id": "trial", "suite_sha256": digest(source.read_bytes())},
        provenance={"source_sha256": source_digest()},
        excerpt_strategy="segments",
    )
    folder = batch / "trial/record"
    folder.mkdir(parents=True)
    row = {
        "id": "trial",
        "task_id": "mcp_stdio",
        "repeat": 1,
        "strategy": "segments",
        "status": "saved",
        "run_file": "trial/record",
        "run_sha256": fingerprint(run),
    }
    plan = {
        "suite_sha256": digest(source.read_bytes()),
        "source_sha256": source_digest(),
        "trials": [row],
        "mode": "frozen",
    }
    (batch / "suite.json").write_bytes(source.read_bytes())
    for path, data in (
        (batch / "plan.json", plan),
        (batch / "index.json", [row]),
        (folder / "run.json", run),
        (folder / "task-review.draft.json", prepare_task_review(run, load_suite(source).tasks[0])),
    ):
        ui.save_json(path, data)
    return run, folder


class EvaluationTerminalTests(unittest.TestCase):
    def test_evaluation_failure_code_is_preserved_by_home(self):
        with (
            patch("builtins.input", side_effect=["4", "0"]),
            patch.object(ui, "run_evaluation_center", return_value=2),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(terminal.run_terminal(), 2)

    def test_new_source_requires_fetch_confirmation_before_any_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            def operation(args, folder, timeout=600):
                ui.save_json(
                    folder / "operation-fixture.stdout.txt",
                    {
                        "mode": "frozen",
                        "trials": [{}],
                        "live_model_trials": 1,
                        "strategies": ["segments"],
                        "answer_styles": ["standard"],
                        "chars_per_page": 2400,
                        "max_answer_chars": 500,
                        "documents": {"new": "https://example.org/"},
                        "corpus": None,
                    },
                )
                return 0

            with (
                patch.object(ui, "run_operation", side_effect=operation) as command,
                patch("builtins.input", return_value=""),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(ui.perform_evaluation([], workspace), 0)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0][0], "plan")

    def test_home_dispatch_has_no_env_or_model_side_effect(self):
        with (
            patch("builtins.input", side_effect=["4", "0", "0"]),
            redirect_stdout(io.StringIO()),
            patch("dotenv.load_dotenv") as env,
            patch("as_researcher.models.live_model") as model,
        ):
            self.assertEqual(terminal.run_terminal(), 0)
        env.assert_not_called()
        model.assert_not_called()

    def test_custom_suite_preserves_question_requirements_and_support_gate(self):
        suite = ui.custom_suite(
            "比较两个API各自的取消对象和总时间限制",
            ["说明第一个API取消哪一个任务", "说明第二个API取消哪一个对象"],
            "python",
            "https://docs.python.org/3.13/library/asyncio-task.html",
            500,
        )
        self.assertEqual(len(suite.tasks[0].rubric), 3)
        self.assertEqual(suite.tasks[0].rubric[-1].dimension, "support")
        self.assertIn("500", suite.tasks[0].question)
        self.assertNotEqual(suite.name, load_suite(ui.DEFAULT_SUITE).name)

    def test_new_question_wizard_saved_without_requests_or_mutating_bank(self):
        original = ui.DEFAULT_SUITE.read_bytes()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(ui, "new_workspace", return_value=Path(tmp)),
            patch(
                "builtins.input",
                side_effect=[
                    "比较wait_for和timeout的取消行为",
                    "",
                    "1",
                    "必须指出wait_for会取消等待对象",
                    "1",
                ],
            ),
            redirect_stdout(io.StringIO()),
            patch.object(ui, "run_operation") as execute,
        ):
            suite, case, corpus = ui.create_custom()
            self.assertTrue(suite.exists())
            self.assertEqual(case, "custom_question")
            self.assertEqual(corpus, ui.CORPUS)
            execute.assert_not_called()
        self.assertEqual(ui.DEFAULT_SUITE.read_bytes(), original)

    def test_configuration_changes_one_axis(self):
        with (
            patch("builtins.input", side_effect=["1", "3", "2", "", " "]),
            redirect_stdout(io.StringIO()),
        ):
            args = ui.configure(ui.DEFAULT_SUITE, "python_compare", ui.CORPUS)
        self.assertEqual(args.count("--strategy"), 1)
        self.assertEqual(args.count("--answer-style"), 2)
        self.assertNotIn("--execute", args)

    def test_plan_only_never_crosses_live_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            calls = []

            def operation(args, workspace, timeout=600):
                calls.append(args)
                ui.save_json(
                    workspace / "operation-fixture.stdout.txt",
                    {
                        "mode": "frozen",
                        "trials": [{}],
                        "live_model_trials": 1,
                        "strategies": ["segments"],
                        "answer_styles": ["standard"],
                        "chars_per_page": 2400,
                        "max_answer_chars": 500,
                        "documents": {},
                        "corpus": None,
                    },
                )
                return 0

            with (
                patch.object(ui, "run_operation", side_effect=operation),
                patch("builtins.input", return_value=""),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(ui.perform_evaluation(["--mode", "frozen"], folder), 0)
            self.assertEqual(len(calls), 1)
            self.assertTrue((folder / "preview-plan.json").exists())

    def test_cancel_before_first_choice_creates_nothing(self):
        with (
            patch("builtins.input", side_effect=["q", "0"]),
            patch.object(ui, "new_workspace") as workspace,
            redirect_stdout(io.StringIO()),
        ):
            self.assertIsNone(ui.run_evaluation_center())
            workspace.assert_not_called()

    def test_batch_read_only_pending_and_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            _, folder = bound_fixture(batch)
            before = {str(p): p.read_bytes() for p in batch.rglob("*") if p.is_file()}
            context = assess_saved_batch(batch)
            self.assertIsNone(context["rows"][0]["assessment"]["passed"])
            self.assertEqual(
                before, {str(p): p.read_bytes() for p in batch.rglob("*") if p.is_file()}
            )
            altered = json.loads((folder / "run.json").read_bytes())
            altered["answer"] = "changed"
            ui.save_json(folder / "run.json", altered)
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                load_bound_batch(batch)

    def test_path_traversal_rejected_before_loading_outside_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            bound_fixture(batch)
            rows = json.loads((batch / "index.json").read_bytes())
            rows[0]["run_file"] = "../outside"
            ui.save_json(batch / "index.json", rows)
            with self.assertRaisesRegex(ValueError, "outside"):
                load_bound_batch(batch)

    def test_missing_run_and_length_and_rubric_have_distinct_guidance(self):
        row = {
            "status": "saved",
            "assessment": {
                "operational": {"checks": {"task_answer_within_limit": False}},
                "quality": {"complete": False, "failed_required": ["fact"]},
            },
        }
        lines = ui.describe_trial(row, task())
        self.assertTrue(any("compact" in s for s in lines))
        self.assertTrue(any("复核未完成" in s for s in lines))
        self.assertTrue(any("标准" in s for s in lines))
        self.assertIn("没有可用run", ui.describe_trial({"status": "not_run"}, task())[0])

    def test_manual_review_unknown_is_not_auto_success(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("builtins.input", side_effect=["UI test fixture", "3", "3", "SAVE"]),
            redirect_stdout(io.StringIO()),
        ):
            folder = Path(tmp)
            self.assertTrue(ui.review_trial(example_run(), task(), folder))
            packet = json.loads((folder / "task-review.json").read_bytes())
            self.assertTrue(all(r["met"] is None for r in packet["criteria"]))

    def test_manual_support_quote_and_previous_review_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            old = b'{"old":true}'
            (folder / "task-review.json").write_bytes(old)
            run = example_run()
            answers = [
                "UI synthetic reviewer",
                "1",
                "范围判断符合",
                "1",
                "not in source",
                "Only valid messages",
                "1",
                "表达清楚",
                "SAVE",
            ]
            with patch("builtins.input", side_effect=answers), redirect_stdout(io.StringIO()):
                self.assertTrue(ui.review_trial(run, task(), folder))
            self.assertEqual(next((folder / "review-history").glob("*.json")).read_bytes(), old)
            self.assertEqual(
                json.loads((folder / "task-review.json").read_bytes())["criteria"][0]["evidence"][
                    0
                ]["quote"],
                "Only valid messages",
            )

    def test_review_cancel_never_writes_labels(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("builtins.input", side_effect=["UI fixture", "2", "不满足这项标准", "3", ""]),
            redirect_stdout(io.StringIO()),
        ):
            self.assertFalse(ui.review_trial(example_run(), task(), Path(tmp)))
            self.assertFalse((Path(tmp) / "task-review.json").exists())

    def test_view_and_comparison_do_not_start_model_or_change_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            bound_fixture(batch)
            before = {str(p): p.read_bytes() for p in batch.rglob("*") if p.is_file()}
            with (
                patch("builtins.input", side_effect=["c", "1", "3", "0", "0"]),
                redirect_stdout(io.StringIO()),
                patch.object(ui, "run_operation") as operation,
            ):
                ui.view_batch(batch)
                operation.assert_not_called()
            self.assertEqual(
                before, {str(p): p.read_bytes() for p in batch.rglob("*") if p.is_file()}
            )

    def test_extraction_view_shows_null_and_actual_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            ui.save_json(
                folder / "results.json",
                {
                    "rows": [
                        {
                            "strategy": "balanced",
                            "case": "case1",
                            "budget": 100,
                            "chars": 10,
                            "target_in_page": False,
                            "all_targets_hit": None,
                            "focus": "missing",
                            "selected": {"content": "real text"},
                        }
                    ]
                },
            )
            output = io.StringIO()
            with patch("builtins.input", side_effect=["1", "1", "0"]), redirect_stdout(output):
                ui.show_extraction(folder)
            self.assertIn("real text", output.getvalue())
            self.assertIn("待定1", output.getvalue())

    def test_command_is_fixed_script_without_shell_and_logs_stay_local(self):
        from types import SimpleNamespace

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                ui.subprocess, "run", return_value=SimpleNamespace(returncode=3)
            ) as process,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                ui.run_operation(["score", "--batch", "path with spaces"], Path(tmp)), 3
            )
            call = process.call_args
            self.assertEqual(call.args[0][-1], "path with spaces")
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(len(list(Path(tmp).glob("*.txt"))), 2)


if __name__ == "__main__":
    unittest.main()
