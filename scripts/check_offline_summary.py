"""Read saved delivery bytes and contracts; never rerun a step or load .env."""

import hashlib
import importlib.metadata
import json
import math
import re
from pathlib import Path
from zipfile import ZipFile

if __package__:
    from . import inspect_edit_contract as contract
else:
    import inspect_edit_contract as contract

ROOT = Path(__file__).resolve().parents[1]
STEPS = ("local_replay", "mcp_fixture", "saved_edit", "delivery_audit")
COUNTS = {
    "tool_invocations": 2,
    "search_attempts": 1,
    "page_read_attempts": 1,
    "pages_read": 1,
    "blocked_requests": 0,
}
AUDIT_ERRORS = (
    "secret_value_matches",
    "broken_links",
    "invalid_json",
    "invalid_packets",
    "report_mismatches",
    "invalid_revisions",
    "invalid_comparisons",
    "invalid_replays",
    "forbidden_dependencies",
)


def require(value, code):
    if not value:
        raise ValueError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def report_path(value, root=ROOT, recorded_root=None):
    require(isinstance(value, (str, Path)), "invalid_report_path")
    path = Path(value)
    if path.is_absolute() and recorded_root is not None:
        try:
            path = path.relative_to(recorded_root)
        except ValueError:
            raise ValueError("report_path_outside_recorded_root") from None
    elif path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            raise ValueError("report_path_outside_workspace") from None
    require(".." not in path.parts, "report_path_traversal")
    resolved = (root / path).resolve()
    require(
        resolved.is_relative_to((root / "reports").resolve()) and resolved.name != ".env",
        "report_path_outside_reports",
    )
    return resolved


def source_hash(root):
    paths = sorted((root / "src/as_researcher").glob("*.py"))
    return sha(b"".join(p.name.encode() + b"\0" + p.read_bytes() for p in paths)) if paths else None


def implementation_proof(summary, root):
    expected = summary["entry_sha256"]
    require(digest(expected), "invalid_entry_digest")
    version = summary["application_version"]
    require(
        isinstance(version, str) and re.fullmatch(r"0\.[1-9]\d?\.0", version),
        "unsupported_application_version",
    )
    current = root / "scripts/verify_offline.py"
    if (
        current.is_file()
        and sha(current.read_bytes()) == expected
        and (summary["schema_version"] == 1 or source_hash(root) == summary["source_sha256"])
    ):
        return "matching_workspace_source"
    release = root / "reports" / ("release-v0" + version.split(".")[1])
    manifest = json.loads((release / "manifest.json").read_bytes())
    require(
        manifest["application_version"] == version
        and manifest["files"]["scripts/verify_offline.py"] == expected,
        "frozen_entry_mismatch",
    )
    if summary["schema_version"] == 2:
        require(manifest["source_sha256"] == summary["source_sha256"], "frozen_source_mismatch")
    archive = release / ("source-v0" + version.split(".")[1] + ".zip")
    require(sha(archive.read_bytes()) == manifest["archive_sha256"], "frozen_archive_mismatch")
    with ZipFile(archive) as bundle:
        require(
            sha(bundle.read("scripts/verify_offline.py")) == expected, "frozen_entry_bytes_mismatch"
        )
    return "matching_frozen_source_archive"


def check_summary(path, root=ROOT):
    path = report_path(path, root)
    raw = path.read_bytes()
    summary = json.loads(raw)
    schema = summary.get("schema_version")
    require(
        type(schema) is int
        and schema in (1, 2)
        and summary.get("kind") == "offline_delivery_check",
        "unsupported_summary_schema",
    )
    recorded_root = Path(summary["working_directory"])
    require(recorded_root.is_absolute(), "invalid_recorded_root")

    def locate(value):
        return report_path(value, root, recorded_root)

    require(locate(summary["output"]) == path, "summary_location_mismatch")
    require(
        summary["status"] == "passed_program_checks_only" and summary["passed"] is True,
        "summary_not_completed",
    )
    require(
        type(summary["step_timeout_seconds"]) is int and 1 <= summary["step_timeout_seconds"] <= 60,
        "invalid_saved_timeout",
    )
    rows = summary["steps"]
    require(
        isinstance(rows, list) and len(rows) == 4 and tuple(row["id"] for row in rows) == STEPS,
        "step_order_mismatch",
    )
    for row in rows:
        require(
            row["status"] == "passed" and type(row["returncode"]) is int and row["returncode"] == 0,
            "step_not_passed",
        )
        duration = row["duration_ms"]
        require(
            type(duration) in (int, float) and math.isfinite(duration) and duration >= 0,
            "invalid_saved_duration",
        )
        args = row["command"]
        require(
            isinstance(args, list)
            and all(isinstance(arg, str) for arg in args)
            and Path(args[0]).name.lower() in ("python.exe", "python", "python3"),
            "invalid_saved_command",
        )
        expected = {
            "local_replay": ["scripts/evaluate_offline.py"],
            "mcp_fixture": [
                "scripts/verify_offline.py",
                "--mcp-fixture-output",
                summary["mcp_output"],
            ],
            "saved_edit": ["scripts/inspect_edit_contract.py"],
            "delivery_audit": ["scripts/audit_project.py"],
        }
        require(args[1:] == expected[row["id"]], "saved_command_mismatch")
    require(summary["missing_historical_files"] == [], "saved_history_was_missing")
    loaded = contract.load_fixture(root / "eval/edit_contract_v019.json", root)
    require(
        summary["required_historical_files"] == sorted(loaded["bound_files_sha256"]),
        "historical_list_mismatch",
    )
    require(
        rows[0]["result"]
        == {
            "scenarios_passed": 9,
            "artifact": "examples/offline_evaluation.json",
            "mcp_tested_in_this_step": False,
        },
        "local_result_mismatch",
    )
    require(
        rows[2]["result"]
        == {
            "fixture_sha256": loaded["fixture_sha256"],
            "bound_files_checked": 8,
            "rows_inspected": 9,
            "semantic_score": None,
        },
        "saved_edit_result_mismatch",
    )
    mcp_dir = locate(summary["mcp_output"])
    mcp_path = locate(rows[1]["result"]["artifact"])
    require(mcp_path == mcp_dir / "run.json", "mcp_location_mismatch")
    audit_path = locate(rows[3]["result"]["output"])
    require(
        audit_path.name.startswith("audit-") and audit_path.suffix == ".json",
        "invalid_audit_location",
    )
    expected_paths = {"mcp_run": mcp_path, "mcp_report": mcp_dir / "report.md", "audit": audit_path}
    if schema == 1:
        require(
            path == root / "reports/offline-delivery-v020/summary.json"
            and summary["application_version"] == "0.20.0",
            "unsupported_legacy_summary",
        )
        binding_path = root / "reports/observations-v020.json"
        binding = json.loads(binding_path.read_bytes())
        require(
            binding["application_version"] == "0.20.0"
            and binding["entry_sha256"] == summary["entry_sha256"],
            "legacy_binding_mismatch",
        )
        bound = binding["files_sha256"]
        require(
            set(bound)
            == {p.relative_to(root).as_posix() for p in (path, *expected_paths.values())},
            "legacy_binding_paths_mismatch",
        )
        artifacts = {}
        for name, expected in bound.items():
            file = report_path(name, root)
            require(
                digest(expected) and sha(file.read_bytes()) == expected,
                "legacy_artifact_digest_mismatch",
            )
            artifacts[name] = expected
        local_binding = "recorded_count_only_no_original_snapshot"
    else:
        require(digest(summary["source_sha256"]), "invalid_saved_source_digest")
        expected_paths["local_replay_snapshot"] = path.parent / (path.stem + "-local-replay.json")
        entries = summary["artifacts"]
        require(set(entries) == set(expected_paths), "artifact_roles_mismatch")
        artifacts = {}
        for role, expected_path in expected_paths.items():
            entry = entries[role]
            file = report_path(entry["file"], root)
            require(file == expected_path and digest(entry["sha256"]), "artifact_location_mismatch")
            require(sha(file.read_bytes()) == entry["sha256"], "artifact_digest_mismatch")
            artifacts[file.relative_to(root).as_posix()] = entry["sha256"]
        local = json.loads(expected_paths["local_replay_snapshot"].read_bytes())
        expected_cases = {
            "normal",
            "budget_1",
            "duplicate",
            "duplicate_ablation",
            "empty",
            "search_error",
            "invalid_citation",
            "injection",
            "no_tool",
        }
        require(
            local["mode"] == "synthetic_control_flow_only"
            and len(local["results"]) == 9
            and {row["case"] for row in local["results"]} == expected_cases
            and all(row["control_check"] == "PASS" for row in local["results"]),
            "local_snapshot_mismatch",
        )
        local_binding = "bound_generated_snapshot"
    proof = implementation_proof(summary, root)
    from as_researcher.execution import execution_summary
    from as_researcher.researcher import render_report

    run = json.loads(mcp_path.read_bytes())
    require(
        run["mode"] == "offline_fixture"
        and run["reader"] == run["search_provider"] == "fixture"
        and run["tool_transport"] == "mcp"
        and run["fixture_scenario"] == "tool_chain",
        "mcp_fixture_mode_mismatch",
    )
    require(
        run["provider_responses"] == []
        and run["metrics"]["status"] == "completed"
        and run["metrics"]["warnings"] == []
        and type(run["metrics"]["fixture_model_calls"]) is int
        and run["metrics"]["fixture_model_calls"] == 3,
        "mcp_completion_mismatch",
    )
    counts = execution_summary(run)
    require(
        not counts["issues"]
        and counts["counts"] == COUNTS
        and rows[1]["result"]["counts"] == COUNTS
        and all(type(value) is int for value in rows[1]["result"]["counts"].values()),
        "mcp_counts_mismatch",
    )
    require(
        run["mcp"]["discovered_tools"]
        == rows[1]["result"]["discovered_tools"]
        == ["search", "read_public_page"]
        and rows[1]["result"]["fixture_model_calls"] == 3,
        "mcp_tools_mismatch",
    )
    calls = run["mcp"]["calls"]
    require(len(calls) == 2 and all(call["status"] == "ok" for call in calls), "mcp_rpc_mismatch")
    require((mcp_dir / "report.md").read_text("utf-8") == render_report(run), "mcp_report_mismatch")
    audit = json.loads(audit_path.read_bytes())
    require(
        audit["passed"] is True and all(audit[field] == [] for field in AUDIT_ERRORS),
        "saved_audit_failed",
    )
    for key in (
        "files_scanned",
        "local_links_checked",
        "reports_checked",
        "historical_reports_not_reproduced",
    ):
        require(
            type(audit[key]) is int and audit[key] == rows[3]["result"][key],
            "audit_counts_mismatch",
        )
    return {
        "schema_version": 1,
        "kind": "saved_offline_summary_inspection",
        "verified": True,
        "summary_schema": schema,
        "summary_sha256": sha(raw),
        "recorded_application_version": summary["application_version"],
        "current_application_version": importlib.metadata.version(
            "agentscope-deep-researcher-interview"
        ),
        "entry_proof": proof,
        "local_replay_binding": local_binding,
        "artifacts_sha256": artifacts,
        "historical_files_checked": 8,
        "steps_checked": list(STEPS),
        "model_calls": 0,
        "child_processes": 0,
        "semantic_score": None,
        "note": "Read-only saved-byte/contract consistency, not a digital signature, original execution attestation or factual quality score. Recorded commands are data and are never executed. Legacy v020 output hashes are external, and its local replay has no original bound snapshot. .env is not read.",
    }
