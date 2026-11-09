"""Plan-first same-query service diagnostic; --execute uses real search, no model."""

import argparse
import asyncio
import json
import os
import statistics
from pathlib import Path
from urllib.parse import urlsplit

from as_researcher.batch_runtime import atomic_json
from as_researcher.benchmark import digest, source_digest
from as_researcher.core import ResearchConfig
from as_researcher.researcher import ResearchSession
from as_researcher.search import make_search

if __package__:
    from .benchmark_research import new_output
else:
    from benchmark_research import new_output

ROOT = Path(__file__).resolve().parents[1]


def make_plan(path: Path) -> dict:
    if path.stat().st_size > 32000:
        raise ValueError("Search diagnostic suite too large")
    suite = json.loads(path.read_bytes())
    if (
        suite.get("schema_version") != 1
        or suite.get("providers") != ["duckduckgo", "tavily"]
        or type(suite.get("repeats")) is not int
        or not 1 <= suite["repeats"] <= 2
        or type(suite.get("result_limit")) is not int
        or not 1 <= suite["result_limit"] <= 5
        or type(suite.get("search_timeout")) is not int
        or not 1 <= suite["search_timeout"] <= 20
        or not isinstance(suite.get("queries"), list)
        or not 1 <= len(suite["queries"]) <= 3
    ):
        raise ValueError("Invalid bounded search diagnostic suite")
    identities = set()
    trials = []
    for i, case in enumerate(suite["queries"]):
        if (
            not isinstance(case.get("id"), str)
            or not case["id"].isidentifier()
            or case["id"] in identities
            or not isinstance(case.get("query"), str)
            or not 1 <= len(case["query"].strip()) <= 400
            or not isinstance(case.get("expected_hosts"), list)
            or not all(isinstance(h, str) and h and "/" not in h for h in case["expected_hosts"])
        ):
            raise ValueError("Invalid search query or host criterion")
        identities.add(case["id"])
        for repeat in range(1, suite["repeats"] + 1):
            order = suite["providers"] if (i + repeat) % 2 else suite["providers"][::-1]
            for provider in order:
                trials.append(
                    {
                        "id": f"{case['id']}__{repeat}__{provider}",
                        "provider": provider,
                        "case_id": case["id"],
                        "repeat": repeat,
                        "query": case["query"],
                        "expected_hosts": case["expected_hosts"],
                    }
                )
    return {
        "kind": "search_provider_comparison_plan",
        "source_sha256": source_digest(),
        "entry_sha256": digest(Path(__file__).read_bytes()),
        "suite_sha256": digest(path.read_bytes()),
        "suite": suite,
        "trials": trials,
        "logical_searches": len(trials),
        "tavily_basic_requests": sum(t["provider"] == "tavily" for t in trials),
        "note": suite.get("note", "Host hits are diagnostic only; no answer quality inference."),
    }


async def execute(plan: dict, output: Path | None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")
    if not os.environ.get("TAVILY_API_KEY"):
        raise ValueError("TAVILY_API_KEY is required; no real search executed")
    folder = new_output(output, "search-provider-comparison")
    atomic_json(folder / "plan.json", plan)
    rows = []
    for trial in plan["trials"]:
        backend = make_search(trial["provider"])
        session = ResearchSession(
            ResearchConfig(
                max_searches=1,
                max_page_reads=0,
                results_per_search=plan["suite"]["result_limit"],
                search_timeout=plan["suite"]["search_timeout"],
            ),
            backend,
        )
        observation = json.loads(
            (await session.search_web(trial["query"], "固定查询服务可用性诊断")).content[0].text
        )
        sources = [
            {"id": s.id, "url": s.url, "title": s.title, "content": s.content}
            for s in session.sources.values()
        ]
        # Exact hostname, never substring matches such as docs.python.org.evil.
        hits = sum(urlsplit(s["url"]).hostname in trial["expected_hosts"] for s in sources)
        row = {
            **trial,
            "status": observation["status"],
            "error_code": observation.get("error_code"),
            "result_count": len(sources),
            "expected_host_hits": hits,
            "sources": sources,
            "trace": session.trace,
            "duration_ms": session.trace[0]["duration_ms"],
            "backend_attempts": session.attempts,
            "model_calls": 0,
            "page_reads": 0,
        }
        rows.append(row)
        atomic_json(folder / "results.json", summarize(plan, rows))
        print(
            json.dumps(
                {
                    k: row[k]
                    for k in (
                        "id",
                        "status",
                        "error_code",
                        "result_count",
                        "expected_host_hits",
                        "duration_ms",
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    report = summarize(plan, rows)
    lines = [
        "# 同查询搜索服务诊断",
        "",
        plan["note"],
        "",
        "|服务|计划|已执行|有结果|目标主机命中trial|错误|耗时中位ms|",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for provider, values in report["providers"].items():
        lines.append(
            f"|{provider}|{values['planned']}|{values['executed']}|{values['nonempty']}|{values['host_hit_trials']}|{values['errors']}|{values['median_ms']}|"
        )
    lines += [
        "",
        "来源数量是规范化去重后的登记数；目标主机命中不保证网页版本或事实支持。",
        "",
        "|查询|重复|服务|状态|错误类别|登记来源|目标主机来源|耗时ms|",
        "|---|---:|---|---|---|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"|{row['case_id']}|{row['repeat']}|{row['provider']}|{row['status']}|{row['error_code'] or ''}|{row['result_count']}|{row['expected_host_hits']}|{row['duration_ms']}|"
        )
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"result": str(folder / "results.json"), "providers": report["providers"]},
            ensure_ascii=False,
        )
    )
    return 0 if all(r["result_count"] > 0 for r in rows) else 3


def summarize(plan: dict, rows: list[dict]) -> dict:
    providers = {}
    for provider in plan["suite"]["providers"]:
        group = [r for r in rows if r["provider"] == provider]
        providers[provider] = {
            "planned": sum(t["provider"] == provider for t in plan["trials"]),
            "executed": len(group),
            "nonempty": sum(r["result_count"] > 0 for r in group),
            "host_hit_trials": sum(r["expected_host_hits"] > 0 for r in group),
            "errors": sum(r["status"] == "error" for r in group),
            "median_ms": round(statistics.median(r["duration_ms"] for r in group), 2)
            if group
            else None,
        }
    return {
        "kind": "search_provider_comparison",
        "plan_sha256": digest(json.dumps(plan, sort_keys=True).encode()),
        "source_sha256": plan["source_sha256"],
        "providers": providers,
        "rows": rows,
        "note": plan["note"],
        "model_calls": 0,
        "page_reads": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=ROOT / "eval/search_provider_suite.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--plan",
        type=Path,
        help="Require this saved preview to match current code/suite before executing",
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = make_plan(args.suite)
        if args.plan:
            frozen = json.loads(args.plan.read_bytes())
            if not isinstance(frozen, dict):
                raise ValueError("Invalid frozen search plan")
            frozen.pop("execution", None)
            if frozen != plan:
                raise ValueError(
                    "Search preview/code/suite changed; create a new plan before execution"
                )
        if not args.execute:
            print(json.dumps({"execution": "plan_only", **plan}, ensure_ascii=False, indent=2))
            return 0
        return asyncio.run(execute(plan, args.output))
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
