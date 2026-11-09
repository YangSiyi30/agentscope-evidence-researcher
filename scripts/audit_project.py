"""Repeatable local delivery checks. Never prints configured secret values."""

import argparse
import hashlib
import importlib.metadata
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

from dotenv import dotenv_values

from as_researcher.comparison import check_comparison, render_comparison
from as_researcher.researcher import render_report
from as_researcher.review import check_review, fingerprint
from as_researcher.revision import revision_integrity_errors


def audit(root: Path) -> dict:
    paths = [
        p
        for directory in ("src", "tests", "scripts", "docs", "examples", "eval", "reports")
        for p in (root / directory).rglob("*")
        if p.is_file() and p.suffix in {".py", ".md", ".txt", ".json", ".jsonl", ".html"}
    ]
    paths += [p for p in root.iterdir() if p.is_file() and p.suffix in {".md", ".toml", ".lock"}]
    secrets = [
        value
        for key, value in dotenv_values(root / ".env", encoding="utf-8-sig").items()
        if "API_KEY" in key and value and len(value) >= 8
    ]
    summary = {
        "at": datetime.now(UTC).isoformat(),
        "files_scanned": len(paths),
        "configured_key_count": len(secrets),
        "secret_value_matches": [],
        "local_links_checked": 0,
        "broken_links": [],
        "invalid_json": [],
        "packets_checked": 0,
        "invalid_packets": [],
        "reports_checked": 0,
        "historical_reports_not_reproduced": 0,
        "report_mismatches": [],
        "revision_records_checked": 0,
        "invalid_revisions": [],
        "comparisons_checked": 0,
        "invalid_comparisons": [],
        "replay_sources_checked": 0,
        "invalid_replays": [],
    }
    documents, runs, packets = {}, {}, []
    for path in paths:
        body = path.read_text(encoding="utf-8-sig")
        relative = path.relative_to(root).as_posix()
        if any(secret in body for secret in secrets):
            summary["secret_value_matches"].append(relative)
        # Only maintained docs: saved evidence may literally contain Markdown links.
        if path.suffix == ".md" and (path.parent == root or root / "docs" in path.parents):
            for target in re.findall(r"\]\(([^)]+)\)", body):
                target = target.strip("<>").split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                summary["local_links_checked"] += 1
                if not (path.parent / unquote(target)).exists():
                    summary["broken_links"].append({"file": relative, "target": target})
        if path.suffix != ".json":
            continue
        try:
            data = json.loads(body)
        except ValueError:
            summary["invalid_json"].append(relative)
            continue
        if not isinstance(data, dict):
            continue
        documents[path] = data
        if path.name == "run.json" and "answer" in data:
            runs[fingerprint(data)] = data
        elif "run_sha256" in data and "units" in data:
            packets.append((path, data))
    for path, packet in packets:
        summary["packets_checked"] += 1
        run = runs.get(packet["run_sha256"])
        try:
            valid = run is not None and check_review(run, packet)["valid"]
        except (TypeError, ValueError, KeyError, AttributeError):
            valid = False
        if not valid:
            summary["invalid_packets"].append(path.relative_to(root).as_posix())
    for path, run in documents.items():
        if path.name == "replay.json" and run.get("mode") == "saved_evidence_revision_replay":
            summary["replay_sources_checked"] += 1
            try:
                raw = path.with_name("source-run.json").read_bytes()
                manifest_bytes = (path.parent.parent / "manifest.json").read_bytes()
                manifest = json.loads(manifest_bytes)
                case = next(c for c in manifest["cases"] if c["id"] == run["case_id"])
                valid = (
                    hashlib.sha256(raw).hexdigest()
                    == run["source_file_sha256"]
                    == case["run_file_sha256"]
                    and hashlib.sha256(manifest_bytes).hexdigest() == run["manifest_sha256"]
                    and hashlib.sha256(json.loads(raw)["answer"].encode()).hexdigest()
                    == case["original_answer_sha256"]
                )
                if run["status"] != "setup_error":
                    revision_record = documents[path.with_name("revision.json")]
                    valid = (
                        valid
                        and revision_record["original_answer"] == json.loads(raw)["answer"]
                        and run["status"] == revision_record["status"]
                    )
                    if "generation_parameters" in revision_record:
                        valid = (
                            valid
                            and revision_record["thinking_mode"]
                            == manifest.get("revision_thinking", "provider-default")
                            and revision_record["budget"]["output_tokens"]
                            == manifest["revision_max_tokens"]
                        )
                    if manifest.get("expected_model") and revision_record["attempts"]:
                        valid = valid and revision_record["model"] == manifest["expected_model"]
                    if manifest.get("expected_prompt_sha256") and revision_record["attempts"]:
                        valid = (
                            valid
                            and revision_record["prompt_sha256"]
                            == manifest["expected_prompt_sha256"]
                        )
                    if case.get("expected_input_sha256") and revision_record["attempts"]:
                        valid = (
                            valid
                            and revision_record["input_sha256"] == case["expected_input_sha256"]
                        )
            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                valid = False
            if not valid:
                summary["invalid_replays"].append(path.relative_to(root).as_posix())
        if run.get("kind") == "revision_comparison":
            summary["comparisons_checked"] += 1
            try:
                source = documents[path.with_name("source-run.json")]
                revision_record = documents[path.with_name("revision.json")]
                valid = check_comparison(source, revision_record, run)["valid"]
                html = path.with_suffix(".html")
                if valid and html.exists():
                    valid = html.read_text("utf-8") == render_comparison(
                        source, revision_record, run
                    )
            except (OSError, ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                summary["invalid_comparisons"].append(path.relative_to(root).as_posix())
        revision = (
            run.get("revision")
            if path.name == "run.json"
            else run
            if path.name == "revision.json"
            else None
        )
        if isinstance(revision, dict):
            summary["revision_records_checked"] += 1
            try:
                errors = revision_integrity_errors(
                    revision, run.get("answer") if path.name == "run.json" else None
                )
                for key, name in (
                    ("original_answer", "answer.original.md"),
                    ("candidate_answer", "answer.candidate.md"),
                ):
                    if isinstance(revision.get(key), str):
                        saved = path.with_name(name)
                        if not saved.exists() or saved.read_text("utf-8") != revision[key]:
                            errors.append(name + "_mismatch")
            except (ValueError, TypeError, KeyError):
                errors = ["invalid_revision_record"]
            if errors:
                summary["invalid_revisions"].append(
                    {"path": path.relative_to(root).as_posix(), "errors": errors}
                )
        if path.name != "run.json" or "answer" not in run:
            continue
        # Pre-v0.4 renderer contracts were not versioned. Preserve their original outputs.
        if run.get("schema_version", 0) < 4:
            summary["historical_reports_not_reproduced"] += 1
            continue
        summary["reports_checked"] += 1
        report = path.with_name("report.md")
        if not report.exists() or report.read_text("utf-8") != render_report(run):
            summary["report_mismatches"].append(path.relative_to(root).as_posix())
    summary["forbidden_dependencies"] = sorted(
        d.metadata["Name"]
        for d in importlib.metadata.distributions()
        if d.metadata.get("Name", "").lower().startswith(("langchain", "langgraph"))
    )
    summary["passed"] = not any(
        summary[key]
        for key in (
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
    )
    summary["note"] = (
        "Known configured API-key matches only, not complete secret detection. Packet validity is mechanical; no human identity or factual quality certification."
    )
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = audit(root)
    target = (
        root
        / "reports"
        / ("audit-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6] + ".json")
    )
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({"output": str(target), **result}, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
