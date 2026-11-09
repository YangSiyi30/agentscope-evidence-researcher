"""Diagnostic reports preserve unknowns and originals, not inferred root causes."""

import csv
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from as_researcher import diagnostics as diag
from as_researcher import evaluation_terminal as ui
from as_researcher.benchmark import digest, load_suite, prepare_task_review
from scripts import restore_test_fixtures as fixtures
from scripts.benchmark_research import main
from tests.test_evaluation_terminal import bound_fixture


class DiagnosticsTests(unittest.TestCase):
    def test_frozen_report_is_not_mislabeled_as_scripted_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            bound_fixture(batch)
            report = diag.diagnose_batch(batch)
            self.assertIn("当时真实模型回答", diag.render_diagnostics(report))
            self.assertNotIn("回答预写", diag.render_diagnostics(report))
            report["mode"] = "fixture"
            self.assertIn("回答预写", diag.render_diagnostics(report))

    def test_readonly_deterministic_unknowns_and_exact_file_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            bound_fixture(batch)
            before = {p: p.read_bytes() for p in batch.rglob("*") if p.is_file()}
            with (
                patch("dotenv.load_dotenv", side_effect=AssertionError("no .env")),
                patch("as_researcher.models.live_model", side_effect=AssertionError("no model")),
                patch.object(Path, "write_text", side_effect=AssertionError("no writes")),
            ):
                report = diag.diagnose_batch(batch)
                self.assertEqual(report, diag.diagnose_batch(batch))
            self.assertEqual(before, {p: p.read_bytes() for p in batch.rglob("*") if p.is_file()})
            self.assertIsNone(report["trials"][0]["passed"])
            self.assertIsNone(report["trials"][0]["failure_layer"])
            self.assertEqual(report["confirmed_failure_layers"], {})
            self.assertTrue(all(c["met"] is None for c in report["criteria"]))
            self.assertTrue(
                all(digest((batch / p).read_bytes()) == h for p, h in report["bindings"].items())
            )

    def test_invalid_review_does_not_export_invented_success_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            run, folder = bound_fixture(batch)
            case = load_suite(batch / "suite.json").tasks[0]
            packet = prepare_task_review(run, case)
            packet["run_sha256"] = "wrong"
            for row in packet["criteria"]:
                row["met"] = True
            ui.save_json(folder / "task-review.json", packet)
            report = diag.diagnose_batch(batch)
            self.assertTrue(
                all(
                    c["met"] is None and c["label_state"] == "invalid_review"
                    for c in report["criteria"]
                )
            )
            self.assertEqual(report["candidate_layers_by_trial"], {"L5": 1})

    def test_valid_negative_review_exports_dimension_without_claiming_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            run, folder = bound_fixture(batch)
            case = load_suite(batch / "suite.json").tasks[0]
            packet = prepare_task_review(run, case)
            packet.update(
                reviewer={"kind": "assistant", "name": "synthetic-test"}, coverage_checked=True
            )
            for row in packet["criteria"]:
                row.update(met=False, reason="Synthetic regression label, no real evaluation.")
            ui.save_json(folder / "task-review.json", packet)
            report = diag.diagnose_batch(batch)
            self.assertEqual(report["groups"]["qa/segments"]["failed"], 1)
            self.assertEqual(report["unlocalized_failed_trials"], 1)
            self.assertTrue(all(c["met"] is False for c in report["criteria"]))
            self.assertIsNone(report["trials"][0]["failure_layer"])
            self.assertEqual(report["candidate_layers_by_trial"]["L4"], 1)

    def test_unrun_is_pending_not_failure_and_not_a_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            bound_fixture(batch)
            index = json.loads((batch / "index.json").read_bytes())
            index[0]["status"] = "not_run"
            ui.save_json(batch / "index.json", index)
            report = diag.diagnose_batch(batch)
            self.assertEqual(report["groups"]["qa/segments"]["unassessed"], 1)
            self.assertTrue(all(c["label_state"] == "not_run" for c in report["criteria"]))

    def test_csv_formula_neutralization_and_unicode(self):
        values = [
            "=1+1",
            " +SUM(A1:A2)",
            "-2+3",
            "@cmd",
            "\tformula",
            "\nformula",
            "正常中文",
            "a,b\n换行",
        ]
        body = diag.csv_text([{"value": s} for s in values], ["value"])
        rows = list(csv.DictReader(io.StringIO(body)))
        self.assertTrue(all(row["value"].startswith("'") for row in rows[:6]))
        self.assertEqual(rows[-1]["value"], values[-1])
        self.assertEqual(rows[-2]["value"], "正常中文")

    def test_html_escapes_answer_and_exports_wide_matrix_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "batch"
            batch.mkdir()
            bound_fixture(batch)
            report = diag.diagnose_batch(batch)
            report["trials"][0]["answer"] = "<script>alert(1)</script>"
            report["criteria"][0]["reason"] = "<img src=x onerror=alert(1)>"
            output = Path(tmp) / "export"
            diag.write_diagnostics(report, output)
            body = (output / "index.html").read_text("utf8")
            self.assertNotIn("<script>", body)
            self.assertNotIn("<img ", body)
            self.assertIn("&lt;script&gt;", body)
            self.assertEqual(len(list(output.glob("matrix-*.csv"))), 1)
            matrix = list(
                csv.DictReader(io.StringIO((output / "matrix-1.csv").read_text("utf-8-sig")))
            )
            self.assertEqual(matrix[0]["mcp_stdio:point_1"], "null")
            with self.assertRaises(FileExistsError):
                diag.write_diagnostics(report, output)

    def test_binding_tamper_is_rejected_before_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            _, folder = bound_fixture(batch)
            run = json.loads((folder / "run.json").read_bytes())
            run["answer"] = "changed"
            ui.save_json(folder / "run.json", run)
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                diag.diagnose_batch(batch)

    def test_review_changed_during_diagnosis_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            bound_fixture(batch)
            original = diag.check_task_review
            with patch.object(
                diag, "check_task_review", side_effect=lambda *a: original(*a) | {"complete": True}
            ):
                with self.assertRaisesRegex(ValueError, "Review changed"):
                    diag.diagnose_batch(batch)

    def test_signal_layer_counts_are_tentative_and_not_duplicate_events(self):
        case = load_suite(ui.DEFAULT_SUITE).tasks[0]
        row = {
            "status": "saved",
            "assessment": {
                "quality": {},
                "operational": {
                    "checks": {
                        "answer_within_limit": False,
                        "task_answer_within_limit": False,
                        "completed": False,
                    }
                },
            },
        }
        signals = diag.trial_signals(row, case)
        self.assertEqual(sum(s["candidate_layers"] == ["L4"] for s in signals), 2)
        self.assertEqual(
            next(s for s in signals if s["code"] == "gate:completed")["candidate_layers"], []
        )

    def test_terminal_d_uses_only_diagnose_and_open_failure_keeps_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "batch"
            batch.mkdir()
            bound_fixture(batch)
            with (
                patch.object(ui, "new_workspace", return_value=Path(tmp) / "export"),
                patch.object(ui, "run_operation", return_value=0) as operation,
                patch.object(ui, "open_local", side_effect=OSError),
                patch("builtins.input", side_effect=["d", "0"]),
                redirect_stdout(io.StringIO()) as out,
            ):
                ui.view_batch(batch)
            self.assertEqual(operation.call_args.args[0][0], "diagnose")
            self.assertIn("打开失败", out.getvalue())

    def test_cli_requires_batch_and_refuses_outside_output(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["diagnose"])
            with tempfile.TemporaryDirectory() as tmp:
                batch = Path(tmp) / "batch"
                batch.mkdir()
                bound_fixture(batch)
                with self.assertRaises(SystemExit):
                    main(
                        ["diagnose", "--batch", str(batch), "--output", str(Path(tmp) / "outside")]
                    )


class FixtureRestoreTests(unittest.TestCase):
    def test_bundle_restores_exact_bytes_and_second_run_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = fixtures.restore(root)
            self.assertEqual(first["restored"], 31)
            second = fixtures.restore(root)
            self.assertEqual(second["restored"], 0)
            self.assertEqual(second["already_identical"], 31)

    def test_existing_difference_refused_before_writing_any_other_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "eval/minimal_correction_v016.json"
            path.parent.mkdir()
            path.write_bytes(b"local modification")
            with self.assertRaisesRegex(ValueError, "differs"):
                fixtures.restore(root)
            self.assertFalse((root / "reports").exists())
            self.assertEqual(path.read_bytes(), b"local modification")

    def test_archive_hash_or_member_hash_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = json.loads(fixtures.MANIFEST.read_bytes())
            manifest["archive_sha256"] = "wrong"
            path = root / "manifest.json"
            ui.save_json(path, manifest)
            with self.assertRaisesRegex(ValueError, "archive fingerprint"):
                fixtures.restore(root, manifest=path)
            manifest = json.loads(fixtures.MANIFEST.read_bytes())
            manifest["files"][next(iter(manifest["files"]))] = "wrong"
            ui.save_json(path, manifest)
            with self.assertRaisesRegex(ValueError, "member fingerprint"):
                fixtures.restore(root, manifest=path)
            self.assertFalse((root / "reports").exists())

    def test_archive_path_traversal_and_alternate_stream_refused(self):
        for name in [
            "reports/../../outside",
            "reports/data:stream",
            "reports\\data",
            "/absolute",
            ".env",
        ]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = root / "unsafe.zip"
                with zipfile.ZipFile(bundle, "w") as archive:
                    archive.writestr(name, b"data")
                manifest = root / "manifest.json"
                ui.save_json(
                    manifest,
                    {
                        "archive_sha256": digest(bundle.read_bytes()),
                        "files": {name: digest(b"data")},
                    },
                )
                with self.assertRaises(ValueError):
                    fixtures.restore(root, bundle, manifest)
                self.assertFalse((root / "reports").exists())


if __name__ == "__main__":
    unittest.main()
