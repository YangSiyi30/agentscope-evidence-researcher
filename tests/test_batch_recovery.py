"""Crash windows, immutable saved runs, single-writer and no implicit paid retries."""

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from as_researcher import batch_runtime as runtime
from as_researcher import evaluation_terminal as ui
from as_researcher.benchmark import (
    assess_saved_batch,
    load_suite,
    prepare_task_review,
    source_digest,
)
from as_researcher.review import fingerprint
from scripts import benchmark_research as command
from tests.test_benchmark import ROOT, example_run


def batch_fixture(folder, repeats=3):
    suite = load_suite(ROOT / "eval/research_suite.json")
    args = SimpleNamespace(
        case=["mcp_stdio"],
        strategy=["segments"],
        mode="fixture",
        transport="local",
        corpus=ROOT / "reports/evaluation-20261101/corpus-exposed",
        operation="run",
        repeats=repeats,
        suite=ROOT / "eval/research_suite.json",
        chars_per_page=800,
        answer_chars=500,
        timeout=30,
        model_max_tokens=1800,
        model_thinking="provider-default",
        search_provider="duckduckgo",
    )
    plan = command.make_plan(args, suite)
    folder.mkdir()
    command.write_json(folder / "plan.json", plan)
    (folder / "suite.json").write_bytes(args.suite.read_bytes())
    rows = copy.deepcopy(plan["trials"])
    command.write_json(folder / "index.json", rows)
    runtime.seal_execution(folder, plan, ROOT)
    return plan, rows


def receipt_fixture(batch, plan, row, status="running", reserve=True):
    if reserve:
        runtime.reserve_trial(batch, row, plan)
    row["status"] = status
    run = example_run()
    run["metrics"].update(pages_read=1, stop_reason="agent_completed")
    run.update(
        benchmark={
            "trial_id": row["id"],
            "suite_sha256": plan["suite_sha256"],
            "corpus_manifest_sha256": plan["corpus_manifest_sha256"],
        },
        provenance={"source_sha256": source_digest()},
        excerpt_strategy=row["strategy"],
    )
    folder = batch / row["id"] / "record"
    folder.mkdir(parents=True)
    command.write_json(folder / "run.json", run)
    case = load_suite(batch / "suite.json").tasks[0]
    command.write_json(folder / "task-review.draft.json", prepare_task_review(run, case))
    receipt = {
        "run_file": folder.relative_to(batch).as_posix(),
        "run_sha256": fingerprint(run),
        "metrics": run["metrics"],
        "attempt_id": row["attempt_id"],
        "infrastructure_failure": False,
    }
    command.write_json(batch / (row["id"] + ".result.json"), receipt)
    return folder, receipt


class RecoveryTests(unittest.TestCase):
    def test_receipt_accepts_equivalent_root_spelling_but_rejects_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alias").mkdir()
            batch = root / "alias" / ".." / "batch"
            plan, rows = batch_fixture(batch, 1)
            _, receipt = receipt_fixture(batch, plan, rows[0])
            self.assertEqual(runtime.validate_receipt(batch, rows[0], plan), receipt)
            receipt["run_file"] = "../outside"
            command.write_json(batch / (rows[0]["id"] + ".result.json"), receipt)
            with self.assertRaisesRegex(ValueError, "outside trial"):
                runtime.validate_receipt(batch, rows[0], plan)

    def test_running_and_interrupted_are_unassessed_not_premature_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "batch"
            plan, rows = batch_fixture(batch, 2)
            rows[0]["status"] = "running"
            rows[1]["status"] = "interrupted"
            command.write_json(batch / "index.json", rows)
            report = assess_saved_batch(batch)
            self.assertTrue(all(row["assessment"]["passed"] is None for row in report["rows"]))
            self.assertEqual(report["summary"]["strategies"]["segments"]["unassessed"], 2)
            self.assertEqual(report["summary"]["strategies"]["segments"]["failed"], 0)

    def test_graceful_chunk_returns_four_and_never_reserves_next_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "batch"
            plan, rows = batch_fixture(batch, 2)

            def child(argv, **kwargs):
                receipt_fixture(batch, plan, rows[0], reserve=False)
                return SimpleNamespace(returncode=23)

            with (
                patch.object(command.subprocess, "run", side_effect=child) as process,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(command.run_rows(batch, plan, rows, {r["id"] for r in rows}, 1), 4)
            self.assertEqual(process.call_count, 1)
            self.assertEqual([r["status"] for r in rows], ["saved", "not_run"])
            self.assertFalse((batch / "attempts" / (rows[1]["id"] + ".json")).exists())

    def test_os_releases_lock_after_child_abrupt_exit(self):
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            code = "import os,sys;from pathlib import Path;from as_researcher.batch_runtime import batch_lock\nwith batch_lock(Path(sys.argv[1])):os._exit(23)"
            child = subprocess.run(
                [sys.executable, "-c", code, tmp],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            self.assertEqual(child.returncode, 23)
            with runtime.batch_lock(Path(tmp)):
                pass

    def test_empty_duplicate_or_unknown_status_never_becomes_vacuous_success(self):
        for case in ["empty", "duplicate", "status"]:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                batch = Path(tmp) / "batch"
                plan, rows = batch_fixture(batch, 2)
                if case == "empty":
                    plan["trials"] = []
                    rows = []
                elif case == "duplicate":
                    plan["trials"][1] = copy.deepcopy(plan["trials"][0])
                    rows = copy.deepcopy(plan["trials"])
                else:
                    rows[0]["status"] = "typo"
                command.write_json(batch / "plan.json", plan)
                command.write_json(batch / "index.json", rows)
                with self.assertRaises(ValueError):
                    runtime.recovery_preview(batch, Path(command.__file__))

    def test_terminal_resume_cancel_or_legacy_does_not_execute(self):
        for legacy in [False, True]:
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as tmp:
                batch = Path(tmp) / "batch"
                plan, rows = batch_fixture(batch, 1)
                if legacy:
                    plan.pop("execution_protocol")
                    command.write_json(batch / "plan.json", plan)
                with (
                    patch("builtins.input", side_effect=["u", "0"] if legacy else ["u", "0", "0"]),
                    patch.object(ui, "run_operation") as execute,
                    redirect_stdout(io.StringIO()),
                ):
                    ui.view_batch(batch)
                execute.assert_not_called()

    def test_preview_is_readonly_no_environment_or_subprocess_and_never_passes_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "batch"
            plan, rows = batch_fixture(batch)
            receipt_fixture(batch, plan, rows[0])
            runtime.reserve_trial(batch, rows[1], plan)
            command.write_json(batch / "index.json", rows)
            before = {p: p.read_bytes() for p in batch.rglob("*") if p.is_file()}
            with (
                patch.object(runtime, "model_binding", side_effect=AssertionError("no env")),
                patch("dotenv.load_dotenv", side_effect=AssertionError("no env")),
                patch.object(command.subprocess, "run", side_effect=AssertionError("no child")),
            ):
                report = runtime.recovery_preview(batch, Path(command.__file__))
            self.assertEqual(report["pending"], [rows[2]["id"]])
            self.assertEqual(report["uncertain"], [rows[1]["id"]])
            self.assertEqual(list(report["recoverable"]), [rows[0]["id"]])
            self.assertEqual(before, {p: p.read_bytes() for p in batch.rglob("*") if p.is_file()})

    def test_resume_adopts_receipt_and_skips_started_trial_without_new_call(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as tmp:
            batch = Path(tmp) / "batch"
            plan, rows = batch_fixture(batch, 2)
            folder, _ = receipt_fixture(batch, plan, rows[0])
            original = (folder / "run.json").read_bytes()
            runtime.reserve_trial(batch, rows[1], plan)
            command.write_json(batch / "index.json", rows)
            with (
                patch.object(command.subprocess, "run", side_effect=AssertionError("no retry")),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(command.resume(batch, True), 2)
            saved = json.loads((batch / "index.json").read_bytes())
            self.assertEqual([r["status"] for r in saved], ["saved", "running"])
            self.assertEqual(original, (folder / "run.json").read_bytes())

    def test_only_pristine_pending_is_started_and_saved_hash_unchanged(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as tmp:
            batch = Path(tmp) / "batch"
            plan, rows = batch_fixture(batch, 2)
            folder, receipt = receipt_fixture(batch, plan, rows[0])
            rows[0].update(receipt, status="saved")
            command.write_json(batch / "index.json", rows)
            original = (folder / "run.json").read_bytes()
            with (
                patch.object(
                    command.subprocess, "run", return_value=SimpleNamespace(returncode=2)
                ) as child,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(command.resume(batch, True), 2)
            self.assertEqual(child.call_count, 1)
            self.assertEqual(child.call_args.args[0][-1], rows[1]["id"])
            self.assertEqual(original, (folder / "run.json").read_bytes())

    def test_no_receipt_after_deadline_never_retries_and_reservation_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "batch"
            plan, rows = batch_fixture(batch, 1)
            runtime.reserve_trial(batch, rows[0], plan)
            rows[0]["status"] = "process_deadline"
            command.write_json(batch / "index.json", rows)
            self.assertEqual(runtime.recovery_preview(batch, Path(command.__file__))["pending"], [])
            with self.assertRaises(FileExistsError):
                runtime.reserve_trial(batch, rows[0], plan)

    def test_orphan_folder_or_reservation_blocks_pristine_status(self):
        for kind in ["folder", "reservation"]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                batch = Path(tmp) / "batch"
                plan, rows = batch_fixture(batch, 1)
                if kind == "folder":
                    (batch / rows[0]["id"]).mkdir()
                else:
                    runtime.reserve_trial(batch, copy.deepcopy(rows[0]), plan)
                report = runtime.recovery_preview(batch, Path(command.__file__))
                self.assertEqual(report["pending"], [])
                self.assertEqual(report["uncertain"], [rows[0]["id"]])

    def test_plan_suite_entry_source_corpus_tampering_rejected(self):
        for field in ["plan", "suite", "entry", "source", "manifest", "html"]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                batch = Path(tmp) / "batch"
                plan, rows = batch_fixture(batch)
                if field in {"plan", "suite"}:
                    path = batch / (field + ".json")
                    path.write_bytes(path.read_bytes() + b" ")
                elif field == "entry":
                    entry = Path(tmp) / "entry.py"
                    entry.write_bytes(b"other")
                elif field == "source":
                    plan["source_sha256"] = "wrong"
                    command.write_json(batch / "plan.json", plan)
                    runtime.seal_execution(batch, plan, ROOT)
                else:
                    corpus = Path(tmp) / "corpus"
                    corpus.mkdir()
                    for p in Path(plan["corpus"]).iterdir():
                        if p.is_file():
                            (corpus / p.name).write_bytes(p.read_bytes())
                    plan["corpus"] = str(corpus)
                    command.write_json(batch / "plan.json", plan)
                    runtime.seal_execution(batch, plan, ROOT)
                    path = corpus / ("manifest.json" if field == "manifest" else "mcp_new.html")
                    path.write_bytes(path.read_bytes() + b" ")
                with self.assertRaises(ValueError):
                    runtime.recovery_preview(
                        batch, entry if field == "entry" else Path(command.__file__)
                    )

    def test_receipt_wrong_attempt_run_metrics_or_review_refused(self):
        for field in ["attempt_id", "metrics", "run_sha256", "review", "journal", "path"]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                batch = Path(tmp) / "batch"
                plan, rows = batch_fixture(batch, 1)
                folder, receipt = receipt_fixture(batch, plan, rows[0])
                if field == "review":
                    (folder / "task-review.draft.json").write_text("{}", "utf8")
                elif field == "journal":
                    command.write_json(batch / "attempts" / (rows[0]["id"] + ".json"), {})
                elif field == "path":
                    receipt["run_file"] = "../outside"
                else:
                    receipt[field] = "wrong"
                command.write_json(batch / (rows[0]["id"] + ".result.json"), receipt)
                command.write_json(batch / "index.json", rows)
                with self.assertRaises((ValueError, KeyError)):
                    runtime.recovery_preview(batch, Path(command.__file__))

    def test_old_batches_are_readonly_not_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "batch"
            plan, rows = batch_fixture(batch, 1)
            plan.pop("execution_protocol")
            command.write_json(batch / "plan.json", plan)
            with self.assertRaisesRegex(ValueError, "Legacy"):
                runtime.recovery_preview(batch, Path(command.__file__))

    def test_model_key_rotation_not_bound_but_endpoint_and_name_are(self):
        values = {
            "MODEL_NAME": "model-a",
            "MODEL_BASE_URL": "https://example.org/v1",
            "MODEL_API_KEY": "first-secret",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, values, clear=True):
            root = Path(tmp)
            before = runtime.model_binding("frozen", root)
            os.environ["MODEL_API_KEY"] = "second-secret"
            self.assertEqual(before, runtime.model_binding("frozen", root))
            os.environ["MODEL_NAME"] = "model-b"
            self.assertNotEqual(before, runtime.model_binding("frozen", root))
            self.assertNotIn("secret", json.dumps(before))

    def test_model_binding_changed_blocks_before_spawning(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as tmp:
            batch = Path(tmp) / "batch"
            batch_fixture(batch, 1)
            with (
                patch.object(command, "model_binding", return_value={"changed": True}),
                patch.object(command.subprocess, "run") as child,
            ):
                with self.assertRaisesRegex(ValueError, "Model endpoint"):
                    command.resume(batch, True)
            child.assert_not_called()

    def test_worker_claim_is_once_and_wrong_model_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "batch"
            plan, rows = batch_fixture(batch, 1)
            runtime.reserve_trial(batch, rows[0], plan)
            with patch.object(runtime, "model_binding", return_value={"changed": True}):
                with self.assertRaises(ValueError):
                    runtime.worker_claim(batch, rows[0], plan, ROOT)
            self.assertEqual(
                runtime.worker_claim(batch, rows[0], plan, ROOT), rows[0]["attempt_id"]
            )
            with self.assertRaises(FileExistsError):
                runtime.worker_claim(batch, rows[0], plan, ROOT)

    def test_os_lock_excludes_second_writer_and_releases_after_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            with runtime.batch_lock(batch):
                with self.assertRaisesRegex(ValueError, "already executing"):
                    with runtime.batch_lock(batch):
                        pass
            with runtime.batch_lock(batch):
                pass

    def test_active_orphan_worker_blocks_execution(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as tmp:
            batch = Path(tmp) / "batch"
            batch_fixture(batch, 1)
            with (
                runtime.batch_lock(batch, "worker.lock"),
                patch.object(command.subprocess, "run") as child,
            ):
                with self.assertRaisesRegex(ValueError, "already executing"):
                    command.resume(batch, True)
            child.assert_not_called()

    def test_interrupt_is_persisted_and_remaining_rows_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "batch"
            plan, rows = batch_fixture(batch, 2)
            with patch.object(command.subprocess, "run", side_effect=KeyboardInterrupt):
                self.assertEqual(command.run_rows(batch, plan, rows, {r["id"] for r in rows}), 130)
            self.assertEqual(
                [r["status"] for r in json.loads((batch / "index.json").read_bytes())],
                ["interrupted", "not_run"],
            )

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.json"
            path.write_bytes(b"original")
            with patch.object(runtime.os, "replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    runtime.atomic_json(path, {"new": True})
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_resume_cli_disallows_changed_budget_and_preview_never_executes(self):
        with redirect_stderr(io.StringIO()), patch.object(command, "resume") as resume:
            with self.assertRaises(SystemExit):
                command.main(["resume", "--batch", "x", "--strategy", "bm25"])
            resume.assert_not_called()
            command.main(["resume", "--batch", "x"])
            resume.assert_called_once()
            self.assertFalse(resume.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
