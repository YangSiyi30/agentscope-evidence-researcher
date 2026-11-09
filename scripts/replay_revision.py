"""Frozen saved-run replay. Plan by default; at most two one-call revisions."""

import argparse
import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from as_researcher.comparison import prepare_comparison, render_comparison
from as_researcher.core import ResearchConfig
from as_researcher.evidence import evidence_diagnostics
from as_researcher.models import live_model
from as_researcher.revision import (
    REVISION_PROMPT,
    answer_chars,
    build_revision_input,
    digest,
    revise_answer,
)

ROOT = Path(__file__).resolve().parents[1]


def load_plan(path: Path, case_ids=None, root: Path = ROOT):
    raw_plan = path.read_bytes()
    plan = json.loads(raw_plan)
    if plan.get("schema_version") != 1 or plan.get("mode") != "saved_evidence_revision_replay":
        raise ValueError("Unsupported replay plan")
    if "expected_prompt_sha256" in plan and plan["expected_prompt_sha256"] != digest(
        REVISION_PROMPT
    ):
        raise ValueError("Frozen revision prompt fingerprint mismatch")
    config = ResearchConfig(
        revise_answer=True,
        max_answer_chars=plan["max_answer_chars"],
        revision_max_tokens=plan["revision_max_tokens"],
        revision_timeout=plan["revision_timeout"],
        revision_thinking=plan.get("revision_thinking", "provider-default"),
    )
    cases = plan["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 2:
        raise ValueError("Replay plan requires 1..2 cases")
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids) or any(
        not isinstance(cid, str) or not cid.replace("_", "").isalnum() for cid in ids
    ):
        raise ValueError("Invalid or duplicate case IDs")
    if case_ids is not None:
        if (
            not case_ids
            or len(set(case_ids)) != len(case_ids)
            or any(cid not in ids for cid in case_ids)
        ):
            raise ValueError("Unknown or duplicate selected case ID")
        cases = [next(c for c in cases if c["id"] == cid) for cid in case_ids]
    loaded = []
    for case in cases:
        source = (root / case["run_file"]).resolve()
        if not source.is_relative_to((root / "reports").resolve()) or source.name != "run.json":
            raise ValueError("Replay source must be a run.json within project reports")
        if source.stat().st_size > 2_000_000:
            raise ValueError("Replay source exceeds 2 MB")
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != case["run_file_sha256"]:
            raise ValueError("Frozen source file fingerprint mismatch")
        run = json.loads(raw)
        if run.get("schema_version", 0) < 5 or run.get("metrics", {}).get("status") != "completed":
            raise ValueError("Replay requires a completed schema >=5 source run")
        if run["metrics"].get("stop_reason") == "search_error" or run.get("revision", {}).get(
            "applied"
        ):
            raise ValueError(
                "Use an original completed report, not failed search or an applied revision"
            )
        if digest(run["answer"]) != case["original_answer_sha256"]:
            raise ValueError("Frozen answer fingerprint mismatch")
        if "expected_input_sha256" in case and case["expected_input_sha256"] != digest(
            build_revision_input(
                run["topic"],
                run["answer"],
                run["sources"],
                run.get("page_evidence", []),
                config.max_answer_chars,
            )
        ):
            raise ValueError("Frozen revision input fingerprint mismatch")
        if answer_chars(run["answer"]) <= config.max_answer_chars:
            raise ValueError("Replay source is not over the declared character budget")
        if not evidence_diagnostics(
            run["answer"], run.get("sources", []), run.get("page_evidence", [])
        )["contract_valid"]:
            raise ValueError("Original reference contract invalid")
        if (
            not isinstance(case.get("review_checklist"), list)
            or not case["review_checklist"]
            or any(not isinstance(c, str) or not c.strip() for c in case["review_checklist"])
        ):
            raise ValueError("Freeze a nonempty review checklist before execution")
        loaded.append({"case": case, "run": run, "raw": raw})
    return raw_plan, config, loaded


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


async def execute(raw_plan, config, loaded, output_root: Path):
    target = output_root / (
        "revision-replay-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    )
    target.mkdir(parents=True, exist_ok=False)
    (target / "manifest.json").write_bytes(raw_plan)
    code_hash = hashlib.sha256(
        b"".join(
            p.name.encode() + b"\0" + p.read_bytes()
            for p in sorted((ROOT / "src/as_researcher").glob("*.py"))
        )
    ).hexdigest()
    rows = []
    exit_code = 3  # All applied candidates still require semantic review.
    for item in loaded:
        case, run = item["case"], item["run"]
        folder = target / case["id"]
        folder.mkdir()
        (folder / "source-run.json").write_bytes(item["raw"])
        metadata = {
            "schema_version": 1,
            "status": "started",
            "mode": "saved_evidence_revision_replay",
            "case_id": case["id"],
            "source_run_file": case["run_file"],
            "source_file_sha256": case["run_file_sha256"],
            "manifest_sha256": hashlib.sha256(raw_plan).hexdigest(),
            "application_source_sha256": code_hash,
            "started_at": datetime.now(UTC).isoformat(),
            "search_calls": 0,
            "page_reads": 0,
            "note": "Historical evidence, one new tool-free model request only; not live retrieval or a new Agent research run.",
        }
        write_json(folder / "replay.json", metadata)
        model = None
        try:
            model = live_model(
                max_retries=0,
                max_tokens=config.revision_max_tokens,
                thinking_mode=config.revision_thinking,
            )
            expected_model = json.loads(raw_plan).get("expected_model")
            if expected_model is not None and model.model != expected_model:
                raise ValueError("Configured model does not match the frozen replay plan")
            revision = await revise_answer(
                run["topic"],
                run["answer"],
                run["sources"],
                run.get("page_evidence", []),
                config,
                model,
            )
        except Exception as exc:
            # Setup exceptions can include endpoint/credential text; keep type only.
            rows.append(
                {"case": case["id"], "status": "setup_error", "error_type": type(exc).__name__}
            )
            metadata.update(status="setup_error", error_type=type(exc).__name__)
            write_json(folder / "replay.json", metadata)
            exit_code = 2
            break
        finally:
            if model is not None:
                await model.client.close()
        write_json(folder / "revision.json", revision)
        metadata["status"] = revision["status"]
        write_json(folder / "replay.json", metadata)
        (folder / "answer.original.md").write_text(revision["original_answer"], "utf-8")
        if revision.get("candidate_answer") is not None:
            (folder / "answer.candidate.md").write_text(revision["candidate_answer"], "utf-8")
        packet = prepare_comparison(run, revision, case["review_checklist"])
        write_json(folder / "comparison.json", packet)
        (folder / "comparison.html").write_text(render_comparison(run, revision, packet), "utf-8")
        row = {
            "case": case["id"],
            "output": str(folder),
            **{
                key: revision.get(key)
                for key in (
                    "status",
                    "applied",
                    "attempts",
                    "original_chars",
                    "candidate_chars",
                    "errors",
                    "token_usage",
                )
            },
        }
        rows.append(row)
        write_json(target / "index.json", rows)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if not revision["applied"]:
            exit_code = 2
        if revision.get("errors") and any(
            e in revision["errors"]
            for e in ("revision_timeout", "revision_model_error", "revision_completion_unverified")
        ):
            break
    if len(rows) != len(loaded):
        exit_code = 2
    write_json(target / "index.json", rows)
    print(
        json.dumps({"output": str(target), "exit_code": exit_code}, ensure_ascii=False), flush=True
    )
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, default=ROOT / "eval/revision_replay_v09.json")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        raw_plan, config, loaded = load_plan(args.plan, args.case_ids)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error("Invalid replay plan or frozen source: " + type(exc).__name__)
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "plan_only",
                    "manifest_sha256": hashlib.sha256(raw_plan).hexdigest(),
                    "cases": [i["case"] for i in loaded],
                    "max_model_calls": len(loaded),
                    "max_answer_chars": config.max_answer_chars,
                    "revision_max_tokens": config.revision_max_tokens,
                    "revision_timeout": config.revision_timeout,
                    "revision_thinking": config.revision_thinking,
                    "search_calls": 0,
                    "page_reads": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")
    for name in ("as", "httpx", "openai"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    return asyncio.run(execute(raw_plan, config, loaded, ROOT / "reports"))


if __name__ == "__main__":
    raise SystemExit(main())
