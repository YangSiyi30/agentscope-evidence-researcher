"""Portable source-only smoke: fixed local/MCP/benchmark/diagnostic commands.

Unlike full historical delivery audit, needs only distributed source + fixtures.
Never loads .env or accepts live-mode flags. Results are program checks, not QA.
"""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from as_researcher.benchmark import digest, source_digest

if __package__:
    from .restore_test_fixtures import ROOT, restore
    from .verify_offline import child_environment
else:
    from restore_test_fixtures import ROOT, restore
    from verify_offline import child_environment


def commands(output):
    """Fixed argv, no shell, source-independent input and no live provider."""
    return [
        (
            "local",
            [
                "-m",
                "as_researcher.cli",
                "离线搜索后读页",
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
                str(output / "local"),
            ],
        ),
        ("mcp", ["scripts/verify_offline.py", "--mcp-fixture-output", str(output / "mcp")]),
        (
            "batch",
            [
                "scripts/benchmark_research.py",
                "run",
                "--execute",
                "--mode",
                "fixture",
                "--suite",
                "eval/research_suite.json",
                "--case",
                "mcp_stdio",
                "--repeats",
                "2",
                "--strategy",
                "segments",
                "--max-new-trials",
                "1",
                "--corpus",
                "reports/evaluation-20261101/corpus-exposed",
                "--output",
                str(output / "batch"),
            ],
        ),
        (
            "resume",
            [
                "scripts/benchmark_research.py",
                "resume",
                "--batch",
                str(output / "batch"),
                "--execute",
            ],
        ),
        (
            "diagnostics",
            [
                "scripts/benchmark_research.py",
                "diagnose",
                "--batch",
                str(output / "batch"),
                "--output",
                str(output / "diagnostics"),
            ],
        ),
    ]


def verify(output):
    output = output.resolve()
    if not output.is_relative_to((ROOT / "reports").resolve()):
        raise ValueError("Output must be a new project reports directory")
    restore()
    output.mkdir(parents=True, exist_ok=False)
    result = {
        "kind": "source_only_offline_smoke",
        "source_sha256": source_digest(),
        "passed": False,
        "steps": [],
        "note": "Program checks only, no model/network quality claims. Full historical delivery audit is separate.",
    }
    initial_runs = {}
    for name, argv in commands(output):
        with (
            (output / (name + ".stdout.log")).open("wb") as stdout,
            (output / (name + ".stderr.log")).open("wb") as stderr,
        ):
            try:
                child = subprocess.run(
                    [sys.executable, *argv],
                    cwd=ROOT,
                    env=child_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=60,
                    shell=False,
                )
                code = child.returncode
            except subprocess.TimeoutExpired:
                code = 124
        expected = 4 if name == "batch" else 0
        result["steps"].append({"id": name, "exit_code": code, "expected_exit_code": expected})
        if code != expected:
            break
        if name == "batch":
            initial_runs = {p: digest(p.read_bytes()) for p in (output / "batch").rglob("run.json")}
    if len(result["steps"]) == 5 and all(
        s["exit_code"] == s["expected_exit_code"] for s in result["steps"]
    ):
        report = json.loads((output / "diagnostics/diagnostics.json").read_bytes())
        checks = {
            "fixture_mode": report["mode"] == "fixture",
            "two_saved_trials": len(report["trials"]) == 2
            and all(t["status"] == "saved" for t in report["trials"]),
            "semantic_still_unknown": all(t["passed"] is None for t in report["trials"]),
            "first_run_unchanged": len(initial_runs) == 1
            and all(p.is_file() and digest(p.read_bytes()) == h for p, h in initial_runs.items()),
            "no_generated_labels": all(c["met"] is None for c in report["criteria"]),
            "html_and_matrix": (output / "diagnostics/index.html").is_file()
            and (output / "diagnostics/matrix-1.csv").is_file(),
        }
        result.update(checks=checks, passed=all(checks.values()))
    result["files_sha256"] = {
        p.relative_to(output).as_posix(): digest(p.read_bytes())
        for p in sorted(output.rglob("*"))
        if p.is_file()
    }
    (output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "summary": str(output / "summary.json"),
                "steps": result["steps"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    output = args.output or ROOT / "reports" / (
        "source-check-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    )
    return verify(output)


if __name__ == "__main__":
    raise SystemExit(main())
