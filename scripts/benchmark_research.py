"""Plan-first research QA and paired experiments. No new framework/dependency.

plan: no model/env/network/write; fetch: bounded public HTML only; ablate: no
model/network; run --execute: isolated bounded trials; score: saved files only.
"""

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from as_researcher.benchmark import (
    STRATEGIES,
    FrozenReader,
    assess_saved_batch,
    digest,
    extract_frozen,
    load_corpus,
    load_suite,
    prepare_blind_review,
    prepare_task_review,
    review_agreement,
    source_digest,
    summarize_trials,
)
from as_researcher.cli import save_run
from as_researcher.core import ResearchConfig
from as_researcher.diagnostics import diagnose_batch, write_diagnostics
from as_researcher.pages import PageReader, fetch_html
from as_researcher.review import fingerprint
from as_researcher.segments import parse_document

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def new_output(value, label):
    path = (
        value
        or ROOT
        / "reports"
        / (label + "-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6])
    ).resolve()
    if not path.is_relative_to((ROOT / "reports").resolve()) or path.exists():
        raise ValueError("Output must be a new directory inside project reports")
    path.mkdir(parents=True, exist_ok=False)
    return path


def selected_tasks(suite, identities):
    identities = identities or ["mcp_stdio"]
    available = {t.id: t for t in suite.tasks}
    if (
        not identities
        or len(set(identities)) != len(identities)
        or any(t not in available for t in identities)
    ):
        raise ValueError("Choose unique existing task IDs")
    return [available[t] for t in identities]


def make_plan(args, suite):
    tasks = selected_tasks(
        suite, [t.id for t in suite.tasks] if getattr(args, "all", False) else args.case
    )
    strategies = args.strategy or ["segments", "bm25"]
    styles = getattr(args, "answer_style", None) or ["standard"]
    if len(set(styles)) != len(styles) or not 1 <= len(styles) <= 2:
        raise ValueError("Choose one or two unique answer styles")
    if len(styles) > 1 and len(strategies) > 1:
        raise ValueError("Vary only one axis: use one strategy for answer-style comparison")
    if len(set(strategies)) != len(strategies) or not 1 <= len(strategies) <= 2:
        raise ValueError("Choose one or two unique strategies")
    if args.mode in {"web", "search"} and any(
        d in suite.synthetic_documents for task in tasks for d in task.documents
    ):
        raise ValueError("Synthetic injection tasks require frozen/fixture mode")
    if args.mode in {"frozen", "fixture"} and args.transport != "local":
        raise ValueError(
            "Snapshot experiments use local tools; test MCP separately in web/search mode"
        )
    documents = {d: suite.documents[d] for task in tasks for d in task.documents}
    corpus_sha = None
    if args.corpus:
        load_corpus(args.corpus, documents)
        corpus_sha = digest((args.corpus / "manifest.json").read_bytes())
    if (
        args.operation == "run"
        and args.mode in {"frozen", "fixture"}
        and documents
        and not args.corpus
    ):
        raise ValueError("Snapshot execution requires --corpus; plan/fetch do not")
    trials = []
    for i, task in enumerate(tasks):
        for repeat in range(1, args.repeats + 1):
            # Alternate AB/BA by both task and repeat; no best-of reruns.
            variants = [(s, style) for s in strategies for style in styles]
            order = variants if (i + repeat) % 2 else variants[::-1]
            for strategy, style in order:
                variant = strategy + (
                    "__" + style if len(styles) > 1 or style != "standard" else ""
                )
                trials.append(
                    {
                        "id": f"{task.id}__{repeat}__{variant}",
                        "task_id": task.id,
                        "repeat": repeat,
                        "strategy": strategy,
                        "answer_style": style,
                        "variant": variant,
                        "status": "not_run",
                    }
                )
    if len(trials) > 40:
        raise ValueError("At most 40 trials; select fewer cases/repeats")
    return {
        "schema_version": 1,
        "kind": "research_benchmark_plan",
        "application_version": importlib.metadata.version("agentscope-deep-researcher-interview"),
        "created_at": datetime.now(UTC).isoformat(),
        "suite_sha256": digest(args.suite.read_bytes()),
        "source_sha256": source_digest(),
        "entry_sha256": digest(Path(__file__).read_bytes()),
        "tasks": [t.model_dump() for t in tasks],
        "documents": documents,
        "mode": args.mode,
        "transport": args.transport,
        "strategies": strategies,
        "answer_styles": styles,
        "corpus": str(args.corpus.resolve()) if args.corpus else None,
        "corpus_manifest_sha256": corpus_sha,
        "chars_per_page": args.chars_per_page,
        "max_answer_chars": args.answer_chars,
        "timeout": args.timeout,
        "model_max_tokens": args.model_max_tokens,
        "model_thinking": args.model_thinking,
        "model_retries": 0,
        "revise_answer": getattr(args, "revise_answer", False),
        "revision_timeout": 30,
        "revision_max_tokens": 1200,
        "search_provider": args.search_provider,
        "max_agent_iters": 6,
        "trials": trials,
        "live_model_trials": len(trials) if args.mode != "fixture" else 0,
        "limits": "Each trial: 0 model retries, max 6 agent iterations; optional one tool-free revision only on overlong completed drafts (30s/1200 tokens). Search mode 1 search/2 pages; other modes 0 searches/max 3 supplied pages. Parent deadline task timeout + 40s, plus 30s if revision enabled; direct child only. Stop after 2 infrastructure failures; retain all unrun rows. Frozen HTML is sent to configured model in real-model frozen mode.",
    }


async def download(args, suite, plan):
    output = new_output(args.output, "research-corpus")
    write_json(output / "plan.json", plan)
    manifest = {
        "schema_version": 1,
        "suite_sha256": plan["suite_sha256"],
        "pages": {},
        "errors": [],
    }
    for name, url in plan["documents"].items():
        try:
            synthetic = name in suite.synthetic_documents
            html = (
                suite.synthetic_documents[name]
                if synthetic
                else await asyncio.wait_for(fetch_html(url), timeout=30)
            )
            raw = html.encode("utf-8")
            (output / (name + ".html")).write_bytes(raw)
            manifest["pages"][name] = {
                "url": url,
                "sha256": digest(raw),
                "retrieved_at": datetime.now(UTC).isoformat(),
                "source_kind": "synthetic" if synthetic else "public_official_html",
            }
        except Exception as exc:
            manifest["errors"].append({"document": name, "error_type": type(exc).__name__})
        write_json(output / "manifest.json", manifest)
        print(json.dumps({"document": name, "saved": name in manifest["pages"]}), flush=True)
    print(json.dumps({"corpus": str(output), "errors": manifest["errors"]}, ensure_ascii=False))
    return 2 if manifest["errors"] else 0


async def worker(batch, trial_id):
    from dotenv import load_dotenv

    from as_researcher.mcp_bridge import MCPBackends
    from as_researcher.models import ScriptedModel, live_model
    from as_researcher.researcher import run_research
    from as_researcher.search import make_search

    plan = json.loads((batch / "plan.json").read_bytes())
    suite = load_suite(batch / "suite.json")
    if (
        source_digest() != plan["source_sha256"]
        or digest(Path(__file__).read_bytes()) != plan.get("entry_sha256")
        or digest((batch / "suite.json").read_bytes()) != plan["suite_sha256"]
    ):
        raise ValueError("Implementation/suite changed after plan")
    trial = next(t for t in plan["trials"] if t["id"] == trial_id)
    task = next(t for t in suite.tasks if t.id == trial["task_id"])
    mode, strategy = plan["mode"], trial["strategy"]
    for name in ("as", "openai", "httpx"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    if mode != "fixture":
        load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")
    urls = [suite.documents[d] for d in task.documents] if mode != "search" else []
    config = ResearchConfig(
        max_searches=1 if mode == "search" else 0,
        max_page_reads=2 if mode == "search" else len(urls),
        results_per_search=3,
        total_timeout=plan["timeout"],
        max_agent_iters=plan["max_agent_iters"],
        chars_per_page=plan["chars_per_page"],
        max_answer_chars=min(plan["max_answer_chars"], task.max_answer_chars),
        answer_style=trial.get("answer_style", "standard"),
        revise_answer=plan.get("revise_answer", False),
        revision_thinking=plan["model_thinking"]
        if plan.get("revise_answer")
        else "provider-default",
    )
    model = (
        ScriptedModel()
        if mode == "fixture"
        else live_model(
            max_retries=0, max_tokens=plan["model_max_tokens"], thinking_mode=plan["model_thinking"]
        )
    )
    revision_model = (
        live_model(max_retries=0, max_tokens=1200, thinking_mode=plan["model_thinking"])
        if config.revise_answer and mode != "fixture"
        else ScriptedModel()
        if config.revise_answer
        else None
    )
    try:
        async with AsyncExitStack() as stack:
            provider = plan["search_provider"] if mode == "search" else "fixture"
            backend = make_search(provider)
            if mode in {"frozen", "fixture"}:
                reader = None
                if urls:
                    corpus = Path(plan["corpus"])
                    if (
                        digest((corpus / "manifest.json").read_bytes())
                        != plan["corpus_manifest_sha256"]
                    ):
                        raise ValueError("Corpus manifest changed")
                    reader = FrozenReader(
                        load_corpus(corpus, {d: suite.documents[d] for d in task.documents}),
                        strategy,
                    )
            else:
                reader = PageReader(strategy=strategy) if config.max_page_reads else None
                if plan["transport"] == "mcp":
                    remote = await stack.enter_async_context(
                        MCPBackends(provider, "http", strategy=strategy)
                    )
                    backend, reader = remote.search, remote.read
            run = await run_research(
                task.question,
                model,
                backend,
                config,
                source_urls=urls,
                reader=reader,
                revision_model=revision_model,
                mode="offline_scripted_snapshot"
                if mode == "fixture"
                else "live_model_snapshot"
                if mode == "frozen"
                else "live",
            )
            run.update(
                tool_transport=plan["transport"],
                reader="snapshot" if mode in {"fixture", "frozen"} else "http",
                excerpt_strategy=strategy,
                search_provider=provider,
                model_retry_budget=0,
                model_max_tokens=None if mode == "fixture" else plan["model_max_tokens"],
                model_thinking=None if mode == "fixture" else plan["model_thinking"],
            )
            run["benchmark"] = {
                "task_id": task.id,
                "suite_sha256": plan["suite_sha256"],
                "corpus_manifest_sha256": plan["corpus_manifest_sha256"],
                "trial_id": trial_id,
                "mode": mode,
            }
            if mode != "fixture" and plan["transport"] == "mcp":
                run["mcp"] = {"discovered_tools": remote.discovered_tools, "calls": remote.calls}
            target = save_run(run, batch / trial_id)
            write_json(target / "task-review.draft.json", prepare_task_review(run, task))
            write_json(
                batch / (trial_id + ".result.json"),
                {
                    "run_file": target.relative_to(batch).as_posix(),
                    "run_sha256": fingerprint(run),
                    "metrics": run["metrics"],
                    "infrastructure_failure": run["metrics"]["status"] != "completed"
                    or run["metrics"]["stop_reason"] == "search_error"
                    or (
                        run["metrics"]["page_read_attempts"] > 0
                        and run["metrics"]["pages_read"] == 0
                    ),
                },
            )
    finally:
        if mode != "fixture":
            await model.client.close()
            if revision_model is not None:
                await revision_model.client.close()


def execute(args, suite, plan):
    output = new_output(args.output, "research-benchmark")
    write_json(output / "plan.json", plan)
    (output / "suite.json").write_bytes(args.suite.read_bytes())
    rows = [dict(t) for t in plan["trials"]]
    write_json(output / "index.json", rows)
    failures = 0
    for row in rows:
        try:
            process = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "_worker", str(output), row["id"]],
                cwd=ROOT,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=plan["timeout"] + 40 + (30 if plan.get("revise_answer") else 0),
            )
            result_path = output / (row["id"] + ".result.json")
            if process.returncode == 0 and result_path.exists():
                row.update(json.loads(result_path.read_bytes()), status="saved")
                failed = row["infrastructure_failure"]
            else:
                row.update(status="worker_failed", exit_code=process.returncode)
                failed = True
        except subprocess.TimeoutExpired:
            row.update(status="process_deadline")
            failed = True
        failures = failures + 1 if failed else 0
        write_json(output / "index.json", rows)
        print(
            json.dumps({k: row.get(k) for k in ("id", "status", "run_file")}, ensure_ascii=False),
            flush=True,
        )
        if failures >= 2:
            break
    print(
        json.dumps(
            {
                "batch": str(output),
                "planned": len(rows),
                "saved": sum(r["status"] == "saved" for r in rows),
            }
        )
    )
    return (
        2
        if any(
            r["status"] != "saved"
            or r["metrics"]["status"] != "completed"
            or r.get("infrastructure_failure") is True
            for r in rows
        )
        else 0
    )


def score(batch):
    context = assess_saved_batch(batch)
    plan, tasks, rows = context["plan"], context["tasks"], context["rows"]
    summary = {
        "kind": "research_benchmark_assessment",
        "plan_sha256": digest((batch / "plan.json").read_bytes()),
        "source_sha256": plan["source_sha256"],
        "execution_entry_sha256": plan.get("entry_sha256"),
        "grader_source_sha256": source_digest(),
        "grader_entry_sha256": digest(Path(__file__).read_bytes()),
        "mode": plan["mode"],
        "summary": summarize_trials(rows),
        "rows": rows,
    }
    name = "assessment-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    write_json(batch / (name + ".json"), summary)
    lines = [
        "# 研究问答评估",
        "",
        summary["summary"]["note"],
        "",
        f"模式：{plan['mode']}；问题与标准：本目录suite.json；原始计划与顺序：plan.json。",
        "",
        "|策略|计划|保存run|运行门槛通过|完整复核|已通过|已失败|待判定|",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, item in summary["summary"]["strategies"].items():
        lines.append(
            f"|{key}|{item['planned']}|{item['runs_saved']}|{item['operational_passes']}|{item['reviewed']}|{item['passed']}|{item['failed']}|{item['unassessed']}|"
        )
    lines += ["", "## 每次试验的诊断", ""]
    for row in rows:
        assessment = row.get("assessment", {})
        lines += [
            f"### {row['id']}",
            "",
            f"状态：{row['status']}；综合判定：{assessment.get('passed')}。",
            "",
        ]
        quality = assessment.get("quality", {})
        if row.get("run_file"):
            lines += [f"原件：{row['run_file']}/run.json；复核：{row.get('review_file', '')}。", ""]
        if quality.get("errors"):
            lines += ["复核结构错误：" + ", ".join(quality["errors"]), ""]
        lines += ["- " + text for text in quality.get("improvements", [])]
        gates = assessment.get("operational", {}).get("checks", {})
        lines += (
            [
                "运行门槛未通过："
                + ", ".join(key for key, value in gates.items() if value is False),
                "",
            ]
            if any(value is False for value in gates.values())
            else []
        )
        if row.get("run_file"):
            folder = batch / row["run_file"]
            answer = json.loads((folder / "run.json").read_bytes())["answer"]
            fence = "`" * max(
                3, max((len(m.group()) + 1 for m in re.finditer(r"`+", answer)), default=3)
            )
            lines += [
                "问题：" + tasks[row["task_id"]].question,
                "",
                "保存的完整回答：",
                "",
                fence + "text",
                answer,
                fence,
                "",
            ]
    (batch / (name + ".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"assessment": str(batch / (name + ".json")), "summary": summary["summary"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    if any(r.get("assessment", {}).get("quality", {}).get("valid") is False for r in rows):
        return 2
    return 0 if all(r.get("assessment", {}).get("passed") is True for r in rows) else 3


def diagnose(batch, output=None):
    """Only saved originals are read; export never changes runs or review labels."""
    report = diagnose_batch(batch)
    output = (output or ROOT / "reports" / (
        "diagnostics-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    )).resolve()
    if not output.is_relative_to((ROOT / "reports").resolve()):
        raise ValueError("Diagnostics output must be a new directory inside project reports")
    write_diagnostics(report, output)
    print(json.dumps({"diagnostics": str(output / "index.html"), "groups": report["groups"],
                      "signals": report["signals_by_trial"], "note": report["note"]},
                     ensure_ascii=False, indent=2))
    return 0


def ablate(args):
    raw = args.extraction_plan.read_bytes()
    plan = json.loads(raw)
    if (
        plan.get("schema_version") != 1
        or not 1 <= len(plan["cases"]) <= 40
        or not 1 <= len(plan["budgets"]) <= 4
        or any(type(b) is not int or not 100 <= b <= 12000 for b in plan["budgets"])
    ):
        raise ValueError("Invalid extraction plan")
    if len({c["id"] for c in plan["cases"]}) != len(plan["cases"]):
        raise ValueError("Duplicate extraction cases")
    for case in plan["cases"]:
        if (
            not isinstance(case["focus"], str)
            or not 1 <= len(case["focus"]) <= 500
            or case["document"] not in plan["documents"]
            or not isinstance(case["targets"], list)
            or not 1 <= len(case["targets"]) <= 8
            or any(not isinstance(t, str) or not t.strip() or len(t) > 500 for t in case["targets"])
        ):
            raise ValueError("Invalid extraction focus/target/document")
    corpus = load_corpus(args.corpus, plan["documents"])
    rows = []
    for case in plan["cases"]:
        html = corpus[plan["documents"][case["document"]]]

        # Reference membership uses the same normalized document as structured offsets.
        def normalized(text):
            return " ".join(text.split()).casefold()

        full = normalized(parse_document(html)["text"])
        available = all(normalized(t) in full for t in case["targets"])
        for budget in plan["budgets"]:
            for strategy in STRATEGIES:
                timings = []
                for _ in range(5):
                    started = time.perf_counter()
                    selected = extract_frozen(html, strategy, case["focus"], budget)
                    timings.append((time.perf_counter() - started) * 1000)
                text = selected["content"]
                rows.append(
                    {
                        "case": case["id"],
                        "focus": case["focus"],
                        "source_kind": case["source_kind"],
                        "strategy": strategy,
                        "budget": budget,
                        "chars": len(text),
                        "within_budget": len(text) <= budget,
                        "target_in_page": available,
                        "all_targets_hit": all(
                            normalized(t) in normalized(text) for t in case["targets"]
                        )
                        if available
                        else None,
                        "extraction_ms_median": round(statistics.median(timings), 3),
                        "selected": selected,
                    }
                )
    output = new_output(args.output, "research-extraction")
    (output / "plan.json").write_bytes(raw)
    result = {
        "kind": "research_extraction_ablation",
        "plan_sha256": digest(raw),
        "source_sha256": source_digest(),
        "corpus_manifest_sha256": digest((args.corpus / "manifest.json").read_bytes()),
        "corpus": str(args.corpus.resolve()),
        "rows": rows,
        "note": "Developer-selected literal span co-occurrence, not semantics/QA quality. Missing targets are null. Same HTML/budget per pair, parse+select timing (5 repeats, warm local process), not web latency or end-to-end model speed.",
    }
    write_json(output / "results.json", result)
    print(
        json.dumps(
            {
                "output": str(output),
                "comparisons": len(rows),
                "unavailable": sum(r["all_targets_hit"] is None for r in rows),
            },
            ensure_ascii=False,
        )
    )
    return 0


def blind_prepare(args):
    batch = args.batch.resolve()
    plan = json.loads((batch / "plan.json").read_bytes())
    suite = load_suite(batch / "suite.json")
    if digest((batch / "suite.json").read_bytes()) != plan["suite_sha256"]:
        raise ValueError("Suite fingerprint mismatch")
    rows = json.loads((batch / "index.json").read_bytes())
    keys = ("id", "task_id", "repeat", "strategy", "answer_style", "variant")
    if [{k: r.get(k) for k in keys} for r in rows] != [
        {k: r.get(k) for k in keys} for r in plan["trials"]
    ]:
        raise ValueError("Planned rows changed")
    samples, origins = [], []
    tasks = {t.id: t for t in suite.tasks}
    for row in rows:
        if row["status"] != "saved":
            continue
        folder = (batch / row["run_file"]).resolve()
        if not folder.is_relative_to((batch / row["id"]).resolve()):
            raise ValueError("Run outside trial folder")
        run = json.loads((folder / "run.json").read_bytes())
        if (
            fingerprint(run) != row["run_sha256"]
            or run.get("benchmark", {}).get("trial_id") != row["id"]
            or run.get("benchmark", {}).get("suite_sha256") != plan["suite_sha256"]
            or run.get("provenance", {}).get("source_sha256") != plan["source_sha256"]
            or run.get("excerpt_strategy") != row["strategy"]
            or run.get("config", {}).get("answer_style", "standard")
            != row.get("answer_style", "standard")
        ):
            raise ValueError("Run binding mismatch")
        samples.append((tasks[row["task_id"]], run))
        origins.append(
            {
                "run_file": str(folder / "run.json"),
                "trial_id": row["id"],
                "variant": row.get("variant", row["strategy"]),
            }
        )
    packet, mapping = prepare_blind_review(samples)
    mapping.update(batch=str(batch), plan_sha256=digest((batch / "plan.json").read_bytes()))
    for binding in mapping["bindings"]:
        binding.update(origins[binding["source_index"]])
    output = new_output(args.output, "research-blind")
    (output / "reviewer").mkdir()
    write_json(output / "reviewer/template.json", packet)
    write_json(output / "private-map.json", mapping)
    print(
        json.dumps(
            {
                "bundle": str(output),
                "samples": len(samples),
                "share_only": "reviewer/template.json",
                "note": "Give each actual reviewer an independent copy; keep private-map.json and original batch away until labels are frozen. No human review performed.",
            },
            ensure_ascii=False,
        )
    )
    return 0


def blind_check(args):
    template = json.loads((args.bundle / "reviewer/template.json").read_bytes())
    mapping = json.loads((args.bundle / "private-map.json").read_bytes())
    if fingerprint(template) != mapping.get("template_sha256"):
        raise ValueError("Blind template changed")
    result = review_agreement(template, *(json.loads(p.read_bytes()) for p in args.review))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if not result["valid"] else 0 if result["complete"] else 3


def main(argv=None):
    # Windows pipes otherwise may use the legacy ANSI code page for Chinese JSON.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_worker":
        try:
            asyncio.run(worker(Path(argv[1]), argv[2]))
            return 0
        except Exception as exc:
            print(json.dumps({"worker_error_type": type(exc).__name__}))
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=["plan", "fetch", "ablate", "run", "score", "diagnose", "blind-prepare", "blind-check"],
        nargs="?",
        default="plan",
    )
    parser.add_argument("--suite", type=Path, default=ROOT / "eval/research_suite.json")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case", action="append")
    selection.add_argument(
        "--all",
        action="store_true",
        help="Explicitly select all 20 tasks; use one strategy/repeat to keep <=40 trials",
    )
    parser.add_argument(
        "--list-cases",
        action="store_true",
        help="Read question bank and rubrics only, no plan execution",
    )
    parser.add_argument("--strategy", action="append", choices=STRATEGIES)
    parser.add_argument("--answer-style", action="append", choices=["standard", "compact"])
    parser.add_argument(
        "--revise-answer",
        action="store_true",
        help="Opt in to at most one tool-free shortening call; keep original/candidate and separately inspect semantics",
    )
    parser.add_argument("--mode", choices=["frozen", "web", "search", "fixture"], default="frozen")
    parser.add_argument("--transport", choices=["local", "mcp"], default="local")
    parser.add_argument("--search-provider", choices=["duckduckgo", "tavily"], default="duckduckgo")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--chars-per-page", type=int, default=2400)
    parser.add_argument("--answer-chars", type=int, default=700)
    parser.add_argument("--timeout", type=float, default=100)
    parser.add_argument("--model-max-tokens", type=int, default=1800)
    parser.add_argument(
        "--model-thinking", choices=["provider-default", "disabled"], default="provider-default"
    )
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--extraction-plan", type=Path, default=ROOT / "eval/extraction_suite.json")
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--review", type=Path, action="append")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.operation == "diagnose":
            if not args.batch:
                raise ValueError("diagnose requires --batch")
            return diagnose(args.batch.resolve(), args.output)
        if args.operation == "blind-prepare":
            if not args.batch:
                raise ValueError("blind-prepare requires --batch")
            return blind_prepare(args)
        if args.operation == "blind-check":
            if not args.bundle or len(args.review or []) != 2:
                raise ValueError("blind-check requires --bundle and exactly two --review files")
            return blind_check(args)
        if (
            not 100 <= args.chars_per_page <= 12000
            or not 100 <= args.answer_chars <= 10000
            or not 0 < args.timeout <= 180
            or not 64 <= args.model_max_tokens <= 4096
        ):
            raise ValueError("Invalid char/token/time budget")
        if args.operation == "score":
            if not args.batch:
                raise ValueError("score requires --batch")
            return score(args.batch.resolve())
        if args.operation == "ablate":
            if not args.corpus:
                raise ValueError("ablate requires --corpus")
            return ablate(args)
        suite = load_suite(args.suite)
        if args.list_cases:
            print(json.dumps([t.model_dump() for t in suite.tasks], ensure_ascii=False, indent=2))
            return 0
        plan = make_plan(args, suite)
        if args.operation == "fetch":
            return asyncio.run(download(args, suite, plan))
        if args.operation == "run" and args.execute:
            return execute(args, suite, plan)
        print(json.dumps({"execution": "plan_only", **plan}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
