"""Plan-only delivery checks; --run-offline executes four fixed, local steps."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zipfile import BadZipFile

ROOT = Path(__file__).resolve().parents[1]
STEPS = ("local_replay", "mcp_fixture", "saved_edit", "delivery_audit")
ENV_NAMES = {
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "COMSPEC",
    "PATHEXT",
    "SYSTEMDRIVE",
    "PROGRAMDATA",
}


def child_environment():
    env = {key: value for key, value in os.environ.items() if key.upper() in ENV_NAMES}
    return env | {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1"}


def command(step, mcp_output):
    commands = {
        "local_replay": [sys.executable, "scripts/evaluate_offline.py"],
        "mcp_fixture": [
            sys.executable,
            "scripts/verify_offline.py",
            "--mcp-fixture-output",
            str(mcp_output),
        ],
        "saved_edit": [sys.executable, "scripts/inspect_edit_contract.py"],
        "delivery_audit": [sys.executable, "scripts/audit_project.py"],
    }
    if step not in commands:
        raise ValueError("unknown_offline_step")
    return commands[step]


def historical_files():
    contract = json.loads((ROOT / "eval/edit_contract_v019.json").read_bytes())
    base = json.loads((ROOT / contract["base_fixture_file"]).read_bytes())
    files = sorted(
        {item["file"] for item in contract["source_binding"].values()}
        | {
            base["source_binding"][key]
            for key in ("revision_file", "answer_file", "source_run_file", "historical_run_file")
        }
    )
    if any(
        Path(name).is_absolute()
        or not (ROOT / name).resolve().is_relative_to((ROOT / "reports").resolve())
        for name in files
    ):
        raise ValueError("invalid_historical_path")
    return files


def plan(output=None):
    target = (
        Path(output)
        if output
        else ROOT
        / "reports"
        / ("offline-check-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6])
        / "summary.json"
    )
    target = (ROOT / target).resolve()
    if not target.is_relative_to((ROOT / "reports").resolve()) or target.suffix != ".json":
        raise ValueError("output_must_be_new_reports_json")
    mcp_output = target.parent / (target.stem + "-mcp-fixture")
    required = historical_files()
    return {
        "schema_version": 2,
        "kind": "offline_delivery_check",
        "status": "planned",
        "application_version": importlib.metadata.version("agentscope-deep-researcher-interview"),
        "entry_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(
            b"".join(
                p.name.encode() + b"\0" + p.read_bytes()
                for p in sorted((ROOT / "src/as_researcher").glob("*.py"))
            )
        ).hexdigest(),
        "artifacts": {},
        "working_directory": str(ROOT),
        "output": str(target),
        "mcp_output": str(mcp_output),
        "steps": [
            {
                "id": step,
                "command": command(step, mcp_output),
                "status": "not_run",
                "returncode": None,
                "duration_ms": None,
            }
            for step in STEPS
        ],
        "required_historical_files": required,
        "missing_historical_files": [name for name in required if not (ROOT / name).is_file()],
        "recovery": "Source ZIP excludes historical reports. Preserve/restore the original reports directory; do not download replacement evidence or change old hashes.",
        "outputs_note": "local_replay refreshes examples; MCP creates a new fixture run; audit creates its own JSON. Only this invocation's new summary is updated. Existing summary targets are refused.",
        "boundary": "No live models/public search/network pages or browser launch. Scripted models and fixture tools exercise AgentScope/MCP. Audit alone reads .env for known-key comparisons, without loading model configuration. Per-child wait timeout is not an absolute process-tree deadline; only the direct child is killed on timeout, not unrelated processes.",
        "passed": None,
    }


def run_command(args, *, timeout):
    # Temporary files avoid waiting for pipe EOF from an orphaned grandchild.
    # Raw child output is neither printed nor copied into the saved summary.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            args, cwd=ROOT, env=child_environment(), shell=False, stdout=stdout, stderr=stderr
        )
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            process.kill()
            process.wait(timeout=5)
            raise
        stdout.seek(0)
        data = stdout.read(4_000_001)
        if len(data) > 4_000_000:
            raise ValueError("step_output_too_large")
        return code, data.decode("utf-8")


def checked_output(step, stdout):
    if step == "local_replay":
        data = json.loads((ROOT / "examples/offline_evaluation.json").read_bytes())
        rows = data["results"]
        expected = {
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
        if (
            data["mode"] != "synthetic_control_flow_only"
            or len(rows) != 9
            or {r["case"] for r in rows} != expected
            or any(r["control_check"] != "PASS" for r in rows)
        ):
            raise ValueError("local_replay_not_passed")
        return {
            "scenarios_passed": 9,
            "artifact": "examples/offline_evaluation.json",
            "mcp_tested_in_this_step": False,
        }
    data = json.loads(stdout)
    if step == "mcp_fixture":
        if (
            data.get("passed") is not True
            or data["counts"]
            != {
                "tool_invocations": 2,
                "search_attempts": 1,
                "page_read_attempts": 1,
                "pages_read": 1,
                "blocked_requests": 0,
            }
            or data["discovered_tools"] != ["search", "read_public_page"]
        ):
            raise ValueError("mcp_fixture_not_passed")
        return {
            key: data[key]
            for key in ("counts", "discovered_tools", "artifact", "fixture_model_calls")
        }
    if step == "saved_edit":
        if (
            data.get("kind") != "offline_edit_contract_inspection"
            or data["model_calls"] != 0
            or data["semantic_score"] is not None
            or len(data["rows"]) != 9
        ):
            raise ValueError("saved_edit_invalid")
        return {
            "fixture_sha256": data["fixture_sha256"],
            "bound_files_checked": len(data["bound_files_sha256"]),
            "rows_inspected": 9,
            "semantic_score": None,
        }
    if data.get("passed") is not True:
        raise ValueError("delivery_audit_failed")
    return {
        key: data[key]
        for key in (
            "output",
            "files_scanned",
            "local_links_checked",
            "reports_checked",
            "historical_reports_not_reproduced",
        )
    }


def persist(target, record):
    # target was exclusively reserved by this invocation before any child ran.
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8")


def capture_artifacts(record, step, result):
    """Bind generated bytes before recording a passed step; old outputs stay intact."""
    target = Path(record["output"])
    files = {}
    if step == "local_replay":
        snapshot = target.parent / (target.stem + "-local-replay.json")
        with snapshot.open("xb") as output:
            output.write((ROOT / "examples/offline_evaluation.json").read_bytes())
        files["local_replay_snapshot"] = snapshot
    elif step == "mcp_fixture":
        folder = Path(record["mcp_output"])
        if Path(result["artifact"]).resolve() != (folder / "run.json").resolve():
            raise ValueError("unexpected_mcp_artifact")
        files.update(mcp_run=folder / "run.json", mcp_report=folder / "report.md")
    elif step == "delivery_audit":
        files["audit"] = Path(result["output"])
    for role, path in files.items():
        path = path.resolve()
        if not path.is_relative_to((ROOT / "reports").resolve()) or path.name == ".env":
            raise ValueError("artifact_outside_reports")
        record["artifacts"][role] = {
            "file": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }


def execute(record, *, timeout=60, runner=run_command):
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("invalid_step_timeout")
    target = Path(record["output"])
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    record["step_timeout_seconds"] = timeout
    record["status"] = "running"
    if record["missing_historical_files"]:
        record.update(status="missing_historical_artifacts", passed=False)
        persist(target, record)
        return 2
    exit_code = 0
    try:
        for row in record["steps"]:
            started = time.perf_counter()
            row["status"] = "running"
            persist(target, record)
            try:
                # Never execute a command supplied in a saved/external plan.
                args = command(row["id"], Path(record["mcp_output"]))
                code, stdout = runner(args, timeout=timeout)
                if type(code) is not int:
                    raise ValueError("invalid_child_returncode")
                row["returncode"] = code
                if code != 0:
                    row["status"] = "failed"
                    exit_code = 1
                else:
                    row["result"] = checked_output(row["id"], stdout)
                    capture_artifacts(record, row["id"], row["result"])
                    row["status"] = "passed"
            except subprocess.TimeoutExpired:
                row.update(status="timeout", error_type="TimeoutExpired")
                exit_code = 1
            except Exception as exc:
                row.update(status="error", error_type=type(exc).__name__)
                exit_code = 1
            finally:
                row["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            if exit_code:
                break
    except KeyboardInterrupt:
        row.update(status="interrupted", error_type="KeyboardInterrupt")
        exit_code = 130
    record["passed"] = exit_code == 0
    record["status"] = (
        "passed_program_checks_only"
        if not exit_code
        else "interrupted"
        if exit_code == 130
        else "failed"
    )
    persist(target, record)
    return exit_code


async def mcp_fixture(target):
    from as_researcher.core import ResearchConfig
    from as_researcher.execution import execution_summary
    from as_researcher.mcp_bridge import MCPBackends
    from as_researcher.models import ScriptedModel
    from as_researcher.researcher import render_report, run_research

    target = target.resolve()
    if not target.is_relative_to((ROOT / "reports").resolve()):
        raise ValueError("fixture_output_outside_reports")
    target.mkdir(exist_ok=False)
    logging.getLogger("as").setLevel(logging.CRITICAL)
    model = ScriptedModel("tool_chain")
    async with MCPBackends("fixture", "fixture", scenario="tool_chain") as remote:
        result = await run_research(
            "离线验证搜索后按来源ID读页",
            model,
            remote.search,
            ResearchConfig(max_searches=1, max_page_reads=1),
            reader=remote.read,
            mode="offline_fixture",
        )
        result.update(
            tool_transport="mcp",
            reader="fixture",
            search_provider="fixture",
            fixture_scenario="tool_chain",
            mcp={"discovered_tools": remote.discovered_tools, "calls": remote.calls},
        )
    result["metrics"]["fixture_model_calls"] = model.calls
    counts = execution_summary(result)
    passed = (
        result["metrics"]["status"] == "completed"
        and not result["metrics"]["warnings"]
        and not counts["issues"]
        and counts["counts"]
        == {
            "tool_invocations": 2,
            "search_attempts": 1,
            "page_read_attempts": 1,
            "pages_read": 1,
            "blocked_requests": 0,
        }
        and all(event["status"] == "ok" for event in remote.calls)
        and len(remote.calls) == 2
    )
    for name, text in (
        ("run.json", json.dumps(result, ensure_ascii=False, indent=2)),
        ("report.md", render_report(result)),
    ):
        with (target / name).open("x", encoding="utf-8", newline="\n") as output:
            output.write(text)
    print(
        json.dumps(
            {
                "passed": passed,
                "counts": counts["counts"],
                "discovered_tools": remote.discovered_tools,
                "artifact": str(target / "run.json"),
                "fixture_model_calls": model.calls,
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--run-offline",
        action="store_true",
        help="Run fixed local checks; no live/paid command is accepted.",
    )
    modes.add_argument(
        "--check-summary",
        type=Path,
        help="Read a completed saved summary; no .env, children or step rerun.",
    )
    parser.add_argument(
        "--output", type=Path, help="New summary JSON under reports; existing targets refused."
    )
    parser.add_argument(
        "--step-timeout", type=int, default=60, help="Direct-child wait timeout, 1..60 seconds."
    )
    parser.add_argument("--mcp-fixture-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.step_timeout <= 60:
        parser.error("invalid_step_timeout")
    try:
        if args.check_summary:
            if args.output or args.mcp_fixture_output:
                parser.error("summary_check_cannot_write_or_run_worker")
            if __package__:
                from .check_offline_summary import check_summary
            else:
                from check_offline_summary import check_summary
            result = check_summary(args.check_summary)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.mcp_fixture_output:
            if args.run_offline or args.output:
                parser.error("fixture_worker_cannot_run_delivery")
            return asyncio.run(mcp_fixture(args.mcp_fixture_output))
        record = plan(args.output)
        code = execute(record, timeout=args.step_timeout) if args.run_offline else 0
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return code
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        BadZipFile,
    ) as exc:
        # Never echo arbitrary child output, paths from exceptions, or key values.
        hint = (
            "Restore original reports and frozen source archive; do not rerun or change old hashes."
            if args.check_summary
            else "Check reports path and installation."
        )
        parser.exit(2, f"Offline check setup failed: {type(exc).__name__}. {hint}\n")


if __name__ == "__main__":
    raise SystemExit(main())
