"""Saved-byte integrity, relocation and rejection of invented success claims."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_offline_summary as check
from scripts import verify_offline as verify


def fixture_tree(root, modern=False):
    binding = json.loads((check.ROOT / "reports/observations-v020.json").read_bytes())
    fixture = json.loads((check.ROOT / "eval/edit_contract_v019.json").read_bytes())
    base = json.loads((check.ROOT / fixture["base_fixture_file"]).read_bytes())
    files = set(binding["files_sha256"]) | {
        "reports/observations-v020.json",
        "reports/release-v020/manifest.json",
        "reports/release-v020/source-v020.zip",
        "eval/edit_contract_v019.json",
        fixture["base_fixture_file"],
    }
    files |= {item["file"] for item in fixture["source_binding"].values()}
    files |= {
        base["source_binding"][key]
        for key in ("revision_file", "answer_file", "source_run_file", "historical_run_file")
    }
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((check.ROOT / name).read_bytes())
    path = root / "reports/offline-delivery-v020/summary.json"
    data = json.loads(path.read_bytes())
    if modern:
        # Synthetic schema2 record for decoder tests; not a claimed execution.
        path = root / "reports/reader-fixture/summary.json"
        path.parent.mkdir()
        data.update(
            schema_version=2,
            application_version="0.21.0",
            # Keep the synthetic output in the same recorded workspace as all
            # other copied absolute paths; the reader rebases them together.
            output=str(Path(data["working_directory"]) / path.relative_to(root)),
            source_sha256=check.source_hash(check.ROOT),
            artifacts={},
        )
        for name in (
            "scripts/verify_offline.py",
            *(
                p.relative_to(check.ROOT).as_posix()
                for p in (check.ROOT / "src/as_researcher").glob("*.py")
            ),
        ):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((check.ROOT / name).read_bytes())
        data["entry_sha256"] = check.sha((root / "scripts/verify_offline.py").read_bytes())
        snapshot = path.with_name("summary-local-replay.json")
        snapshot.write_bytes((check.ROOT / "examples/offline_evaluation.json").read_bytes())
        roles = {
            "local_replay_snapshot": snapshot,
            "mcp_run": root / "reports/offline-delivery-v020/summary-mcp-fixture/run.json",
            "mcp_report": root / "reports/offline-delivery-v020/summary-mcp-fixture/report.md",
            "audit": root / "reports/audit-20261002T122123-4a9fd1.json",
        }
        data["artifacts"] = {
            role: {"file": p.relative_to(root).as_posix(), "sha256": check.sha(p.read_bytes())}
            for role, p in roles.items()
        }
        path.write_text(json.dumps(data), "utf-8")
    return path, data


def rebind(root, data, role):
    item = data["artifacts"][role]
    item["sha256"] = check.sha((root / item["file"]).read_bytes())


class SummaryReadbackTests(unittest.TestCase):
    def test_legacy_uses_external_hashes_and_frozen_source_without_retroactive_claims(self):
        result = check.check_summary(check.ROOT / "reports/offline-delivery-v020/summary.json")
        self.assertTrue(result["verified"])
        self.assertEqual(result["entry_proof"], "matching_frozen_source_archive")
        self.assertEqual(result["local_replay_binding"], "recorded_count_only_no_original_snapshot")
        self.assertEqual(result["child_processes"], result["model_calls"], 0)
        self.assertIsNone(result["semantic_score"])

    def test_readonly_cli_does_not_load_env_spawn_or_write(self):
        original = Path.read_bytes

        def guarded(path):
            self.assertNotEqual(path.name, ".env")
            return original(path)

        with (
            patch.object(Path, "read_bytes", guarded),
            patch("dotenv.load_dotenv", side_effect=AssertionError("no .env")),
            patch("as_researcher.models.live_model", side_effect=AssertionError("no client")),
            patch.object(verify.subprocess, "Popen", side_effect=AssertionError("no child")),
            patch.object(Path, "mkdir", side_effect=AssertionError("no mkdir")),
            patch.object(Path, "write_text", side_effect=AssertionError("no write")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                verify.main(["--check-summary", "reports/offline-delivery-v020/summary.json"]), 0
            )

    def test_relocated_legacy_rebases_paths_without_using_original_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _ = fixture_tree(root)
            result = check.check_summary(path, root)
            self.assertTrue(result["verified"])
            self.assertEqual(result["entry_proof"], "matching_frozen_source_archive")

    def test_new_schema_has_immutable_snapshot_and_four_bound_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _ = fixture_tree(root, True)
            result = check.check_summary(path, root)
            self.assertEqual(result["summary_schema"], 2)
            self.assertEqual(result["local_replay_binding"], "bound_generated_snapshot")
            self.assertEqual(len(result["artifacts_sha256"]), 4)

    def test_missing_reordered_unrun_boolean_exit_or_fake_command_rejected(self):
        mutations = (
            lambda d: d["steps"].pop(),
            lambda d: d["steps"].reverse(),
            lambda d: d["steps"][1].update(status="not_run", returncode=None),
            lambda d: d["steps"][1].update(returncode=False),
            lambda d: d["steps"][0].update(duration_ms=True),
            lambda d: d["steps"][0]["command"].append("--live"),
            lambda d: d.update(passed=False),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path, data = fixture_tree(root, True)
                mutation(data)
                path.write_text(json.dumps(data), "utf-8")
                with self.assertRaises(ValueError):
                    check.check_summary(path, root)

    def test_artifact_byte_changes_and_rebound_wrong_report_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, data = fixture_tree(root, True)
            target = root / data["artifacts"]["mcp_report"]["file"]
            target.write_bytes(target.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "artifact_digest_mismatch"):
                check.check_summary(path, root)
            rebind(root, data, "mcp_report")
            path.write_text(json.dumps(data), "utf-8")
            with self.assertRaisesRegex(ValueError, "mcp_report_mismatch"):
                check.check_summary(path, root)

    def test_rebound_wrong_mcp_counts_still_fail_trace_and_summary_cross_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, data = fixture_tree(root, True)
            target = root / data["artifacts"]["mcp_run"]["file"]
            run = json.loads(target.read_bytes())
            run["metrics"]["search_attempts"] = 2
            target.write_text(json.dumps(run), "utf-8")
            rebind(root, data, "mcp_run")
            path.write_text(json.dumps(data), "utf-8")
            with self.assertRaisesRegex(ValueError, "mcp_counts_mismatch"):
                check.check_summary(path, root)

    def test_rebound_audit_pass_flag_does_not_hide_error_arrays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, data = fixture_tree(root, True)
            target = root / data["artifacts"]["audit"]["file"]
            audit = json.loads(target.read_bytes())
            audit["invalid_json"] = ["example"]
            target.write_text(json.dumps(audit), "utf-8")
            rebind(root, data, "audit")
            path.write_text(json.dumps(data), "utf-8")
            with self.assertRaisesRegex(ValueError, "saved_audit_failed"):
                check.check_summary(path, root)

    def test_rebound_failing_local_snapshot_is_not_nine_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, data = fixture_tree(root, True)
            target = root / data["artifacts"]["local_replay_snapshot"]["file"]
            local = json.loads(target.read_bytes())
            local["results"][0]["control_check"] = "FAIL"
            target.write_text(json.dumps(local), "utf-8")
            rebind(root, data, "local_replay_snapshot")
            path.write_text(json.dumps(data), "utf-8")
            with self.assertRaisesRegex(ValueError, "local_snapshot_mismatch"):
                check.check_summary(path, root)

    def test_paths_escaping_workspace_or_points_to_env_are_rejected(self):
        for value in ("../outside.json", "reports/../.env", ".env"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check.report_path(value)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, data = fixture_tree(root, True)
            data["artifacts"]["audit"]["file"] = "reports/../.env"
            path.write_text(json.dumps(data), "utf-8")
            with self.assertRaises(ValueError):
                check.check_summary(path, root)

    def test_legacy_external_binding_and_frozen_archive_bytes_are_checked(self):
        for target in ("reports/observations-v020.json", "reports/release-v020/source-v020.zip"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path, _ = fixture_tree(root)
                file = root / target
                if file.suffix == ".json":
                    data = json.loads(file.read_bytes())
                    data["files_sha256"]["reports/offline-delivery-v020/summary.json"] = "0" * 64
                    file.write_text(json.dumps(data), "utf-8")
                else:
                    file.write_bytes(file.read_bytes() + b"changed")
                with self.assertRaises(ValueError):
                    check.check_summary(path, root)

    def test_missing_original_history_has_no_network_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _ = fixture_tree(root, True)
            (root / "reports/minimal-edit-20261002T072619-48e3e0/response.txt").unlink()
            with (
                patch("socket.create_connection", side_effect=AssertionError("no download")),
                self.assertRaises(FileNotFoundError),
            ):
                check.check_summary(path, root)

    def test_capture_copies_local_bytes_once_instead_of_binding_mutable_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "reports/check").mkdir(parents=True)
            (root / "examples").mkdir()
            source = root / "examples/offline_evaluation.json"
            source.write_bytes(b'{"fixture":1}')
            data = {"output": str(root / "reports/check/summary.json"), "artifacts": {}}
            with patch.object(verify, "ROOT", root):
                verify.capture_artifacts(data, "local_replay", {})
                source.write_bytes(b'{"fixture":2}')
                with self.assertRaises(FileExistsError):
                    verify.capture_artifacts(data, "local_replay", {})
            item = data["artifacts"]["local_replay_snapshot"]
            self.assertEqual((root / item["file"]).read_bytes(), b'{"fixture":1}')
            self.assertEqual(item["sha256"], check.sha(b'{"fixture":1}'))

    def test_read_flag_cannot_run_write_worker_and_errors_do_not_echo_private_text(self):
        for args in (
            ["--check-summary", "a", "--run-offline"],
            ["--check-summary", "a", "--output", "b"],
            ["--check-summary", "a", "--mcp-fixture-output", "b"],
        ):
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                verify.main(args)
            self.assertEqual(error.exception.code, 2)
        stream = io.StringIO()
        with (
            patch.object(check, "check_summary", side_effect=ValueError("SECRET")),
            contextlib.redirect_stderr(stream),
            self.assertRaises(SystemExit),
        ):
            verify.main(["--check-summary", "a"])
        self.assertNotIn("SECRET", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
