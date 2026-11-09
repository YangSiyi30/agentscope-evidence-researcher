"""Opt-in real-model development experiments on frozen, explicitly synthetic evidence."""

import argparse
import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from string import Formatter
from uuid import uuid4

from dotenv import load_dotenv

from as_researcher.atomic_review import prepare_atomic_review
from as_researcher.cli import save_run
from as_researcher.core import ResearchConfig, Source
from as_researcher.models import live_model
from as_researcher.researcher import SYSTEM_PROMPT, TOOL_OBSERVATION_VERSION, run_research
from as_researcher.revision import revise_answer
from as_researcher.segments import parse_document, select_passages
from as_researcher.worksheet import render_worksheet

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def omission_inputs(fixture):
    """One declared character budget drives the question and executable config."""
    budget = fixture.get("max_answer_chars")
    template = fixture.get("topic_template", "")
    if type(budget) is not int or "topic" in fixture:
        raise ValueError(
            "Omission fixture requires one integer budget and topic_template; legacy topic is not executable"
        )
    fields = [
        (field, spec, conversion)
        for _, field, spec, conversion in Formatter().parse(template)
        if field is not None
    ]
    if fields != [("max_answer_chars", "", None)]:
        raise ValueError("topic_template must contain exactly one plain max_answer_chars field")
    config = ResearchConfig(
        max_searches=1,
        max_page_reads=1,
        max_agent_iters=5,
        total_timeout=75,
        max_answer_chars=budget,
    )
    topic = template.format(max_answer_chars=config.max_answer_chars)
    if not topic.strip() or len(topic) > 2000:
        raise ValueError("Invalid generated topic")
    cases = fixture["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 2:
        raise ValueError("Omission fixture requires 1..2 cases")
    ids = [case["id"] for case in cases]
    if any(not isinstance(cid, str) or not cid.replace("_", "").isalnum() for cid in ids) or len(
        set(ids)
    ) != len(ids):
        raise ValueError("Invalid or duplicate case IDs")
    for case in cases:
        ResearchConfig(**(config.model_dump() | {"chars_per_page": case["chars_per_page"]}))
    return topic, config


async def execute(args, plan):
    target = (
        ROOT
        / "reports"
        / (
            "synthetic-"
            + args.suite
            + "-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid4().hex[:6]
        )
    )
    target.mkdir(parents=True, exist_ok=False)
    write_json(target / "plan.json", plan)
    fixture = plan["manifest"]
    rows = []
    if args.suite == "revision":
        source = Source("Synthetic Sable", fixture["url"], "Synthetic protocol fixture.")
        original = fixture["original_unit"].format(id=source.id) * fixture["original_repeat"]
        page = {"id": source.id, "url": source.url, "content": fixture["evidence"]}
        config = ResearchConfig(
            max_answer_chars=fixture["max_answer_chars"],
            revise_answer=True,
            revision_max_tokens=args.model_max_tokens,
        )
        model = live_model(max_retries=0, max_tokens=args.model_max_tokens)
        try:
            revision = await revise_answer(fixture["topic"], original, [], [page], config, model)
        finally:
            await model.client.close()
        write_json(target / "revision.json", revision)
        (target / "answer.original.md").write_text(original, "utf-8")
        if revision.get("candidate_answer") is not None:
            (target / "answer.candidate.md").write_text(revision["candidate_answer"], "utf-8")
        row = {
            key: revision.get(key)
            for key in ("status", "original_chars", "candidate_chars", "errors", "token_usage")
        }
        rows.append(row)
        code = 3 if revision["applied"] else 2
    else:
        config = ResearchConfig(**plan["research_config"])
        code = 0
        for case in fixture["cases"]:
            if case["id"] not in plan["selected_cases"]:
                continue
            selected = select_passages(
                parse_document(fixture["html"]), fixture["focus"], case["chars_per_page"]
            )
            source = Source(
                "Synthetic demo API",
                "https://example.org/" + case["id"],
                "Fictional API. Read the saved page for demo.shield examples and cancellation conditions.",
            )

            async def search(query, limit):
                return [source]

            async def reader(url, limit, focus=""):
                # Fixed experiment: ignore variable focus; expose the predeclared
                # fragment so only budget cases change. This is not a real search.
                return selected

            model = live_model(max_retries=0, max_tokens=args.model_max_tokens)
            try:
                if fixture.get("expected_model") and model.model != fixture["expected_model"]:
                    raise ValueError("Configured model does not match frozen omission plan")
                run = await run_research(
                    plan["resolved_topic"],
                    model,
                    search,
                    config.model_copy(update={"chars_per_page": case["chars_per_page"]}),
                    reader=reader,
                    mode="live_model_fixture",
                )
            finally:
                await model.client.close()
            run.update(
                evaluation_case=case["id"],
                fixture_manifest_sha256=plan["manifest_sha256"],
                fixture_focus_policy="predeclared_fixed_excerpt",
                tool_transport="local",
                reader="fixture",
            )
            saved = save_run(run, target / case["id"])
            packet = prepare_atomic_review(run)
            write_json(saved / "review.atomic.draft.json", packet)
            (saved / "review.worksheet.md").write_text(render_worksheet(run, packet), "utf-8")
            row = {
                "case": case["id"],
                "run_file": str((saved / "run.json").relative_to(ROOT)),
                "metrics": run["metrics"],
            }
            rows.append(row)
            write_json(target / "index.json", rows)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if run["metrics"]["status"] != "completed":
                code = 2
                break
            if run["metrics"]["warnings"]:
                code = 3
    write_json(target / "index.json", rows)
    print(
        json.dumps({"output": str(target), "exit_code": code, "results": rows}, ensure_ascii=False),
        flush=True,
    )
    return code


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["omission", "revision"], required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--model-max-tokens", type=int, default=1200)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Explicit frozen fixture; omission defaults to the historical v014 plan",
    )
    parser.add_argument(
        "--case", action="append", dest="case_ids", help="Select an omission case; repeat for both"
    )
    args = parser.parse_args(argv)
    if not 64 <= args.model_max_tokens <= 4096:
        parser.error("model token budget must be 64..4096")
    if args.case_ids and args.suite != "omission":
        parser.error("--case is only supported for omission")
    path = args.manifest or ROOT / "eval" / (
        "omission_v014.json" if args.suite == "omission" else "revision_v08.json"
    )
    manifest_bytes = path.read_bytes()
    plan = {
        "mode": "live_model_synthetic_evidence"
        if args.suite == "omission"
        else "live_revision_synthetic_original_and_evidence",
        "manifest": json.loads(manifest_bytes),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "executor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(
            b"".join(
                p.name.encode() + b"\0" + p.read_bytes()
                for p in sorted((ROOT / "src/as_researcher").glob("*.py"))
            )
        ).hexdigest(),
        "model_max_tokens": args.model_max_tokens,
        "model_retries": 0,
        "note": "No live search/browser/MCP. Assistant observation is not human gold. At most two research tasks or one tool-free revision call.",
    }
    if args.suite == "omission":
        try:
            topic, config = omission_inputs(plan["manifest"])
            ids = [c["id"] for c in plan["manifest"]["cases"]]
            selected = args.case_ids if args.case_ids is not None else ids
            if (
                not selected
                or len(set(selected)) != len(selected)
                or any(cid not in ids for cid in selected)
            ):
                raise ValueError("Unknown or duplicate selected case")
            mismatches = []
            for field, actual in (
                ("expected_source_sha256", plan["source_sha256"]),
                ("expected_executor_sha256", plan["executor_sha256"]),
                ("expected_prompt_sha256", hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()),
                ("expected_tool_observation_version", TOOL_OBSERVATION_VERSION),
                ("model_max_tokens", args.model_max_tokens),
            ):
                if field in plan["manifest"] and plan["manifest"][field] != actual:
                    mismatches.append(field)
            if args.execute and mismatches:
                raise ValueError("Frozen fixture mismatch: " + ", ".join(mismatches))
            plan.update(
                resolved_topic=topic,
                research_config=config.model_dump(),
                selected_cases=selected,
                tool_observation_version=TOOL_OBSERVATION_VERSION,
                execution_blockers=mismatches,
            )
        except (ValueError, TypeError, KeyError) as exc:
            parser.error("Invalid omission manifest: " + str(exc))
    if not args.execute:
        print("Plan only. --execute requires a matching frozen plan and may incur model cost.")
        if plan.get("execution_blockers"):
            print("Historical plan differs from current code/configuration; execution is blocked.")
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")
    for name in ("as", "httpx", "openai"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    return asyncio.run(execute(args, plan))


if __name__ == "__main__":
    raise SystemExit(main())
