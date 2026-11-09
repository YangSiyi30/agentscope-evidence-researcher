"""Opt-in real-model test on explicitly synthetic, fixed evidence-level cases."""

import argparse
import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from as_researcher.atomic_review import prepare_atomic_review
from as_researcher.cli import save_run
from as_researcher.core import ResearchConfig, Source
from as_researcher.models import live_model
from as_researcher.pages import PageReadError
from as_researcher.researcher import run_research
from as_researcher.worksheet import render_worksheet

ROOT = Path(__file__).resolve().parents[1]


async def execute(args, manifest_path, manifest):
    batch = (
        ROOT
        / "reports"
        / (
            "evidence-levels-v06-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid4().hex[:6]
        )
    )
    batch.mkdir(parents=True)
    config = ResearchConfig(
        max_searches=1, max_page_reads=1, max_agent_iters=5, max_answer_chars=700, total_timeout=75
    )
    plan = {
        "mode": "live_model_fixture",
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "config": config.model_dump(),
        "model_max_tokens": args.model_max_tokens,
        "note": "Development tests, synthetic evidence; no automatic semantic score.",
    }
    (batch / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")
    rows = []
    for case in manifest["cases"]:

        async def search(query, limit):
            return [
                Source(
                    "Synthetic Aperture example",
                    "https://example.org/" + case["id"],
                    case["snippet"],
                )
            ]

        async def reader(url, limit, focus=""):
            if "page_error" in case:
                raise PageReadError(case["page_error"])
            return case["page"]

        model = live_model(max_retries=0, max_tokens=args.model_max_tokens)
        try:
            run = await run_research(
                case["topic"], model, search, config, reader=reader, mode="live_model_fixture"
            )
        finally:
            await model.client.close()
        run.update(
            evaluation_case=case["id"],
            fixture_manifest_sha256=plan["manifest_sha256"],
            tool_transport="local",
            reader="fixture",
            model_max_tokens=args.model_max_tokens,
        )
        target = save_run(run, batch / case["id"])
        packet = prepare_atomic_review(run)
        (target / "review.atomic.draft.json").write_text(
            json.dumps(packet, ensure_ascii=False, indent=2), "utf-8"
        )
        (target / "review.worksheet.md").write_text(render_worksheet(run, packet), "utf-8")
        row = {
            "case": case["id"],
            "run_file": str((target / "run.json").relative_to(ROOT)),
            "metrics": run["metrics"],
        }
        rows.append(row)
        (batch / "index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if run["metrics"]["status"] != "completed":
            print("Stopped after incomplete model run; no automatic retry.", flush=True)
            break
    print("Saved:", batch, flush=True)
    return (
        0
        if len(rows) == len(manifest["cases"])
        and all(row["metrics"]["status"] == "completed" for row in rows)
        else 2
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--model-max-tokens", type=int, default=1200)
    args = parser.parse_args()
    if not 64 <= args.model_max_tokens <= 4096:
        parser.error("model token budget must be 64..4096")
    path = ROOT / "eval/evidence_levels_v06.json"
    manifest = json.loads(path.read_text("utf-8"))
    if not args.execute:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(
            "Plan only. --execute uses up to 3 real-model tasks with fixed synthetic search/page data."
        )
        return
    load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")
    for name in ("as", "httpx", "openai"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    raise SystemExit(asyncio.run(execute(args, path, manifest)))


if __name__ == "__main__":
    main()
