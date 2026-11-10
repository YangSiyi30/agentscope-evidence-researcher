"""Record native same-response tool batching; fixed local budget/failure cases only."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
from pathlib import Path
from unittest.mock import patch

import httpx
from agentscope.agent import Agent
from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse

from as_researcher.core import ResearchConfig
from as_researcher.execution import execution_summary
from as_researcher.models import ScriptedModel
from as_researcher.researcher import render_report, run_research
from as_researcher.search import TavilySearch

ROOT = Path(__file__).resolve().parents[1]
CASES = ("search_budget", "rate_limit")
QUERIES = ("batch search alpha", "batch search beta", "batch search gamma")
CALL_IDS = ("batch-1", "batch-2", "batch-3")
URLS = tuple(f"https://example.org/batch/{index}" for index in range(1, 4))


def payload(block):
    return json.loads("".join(item["text"] for item in block["output"]))


class BatchModel(ScriptedModel):
    """Return three requests together, then a fixed answer after receiving their results."""

    def __init__(self):
        super().__init__()
        self.turns = []

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        received = [
            block.model_dump(mode="json")
            for message in messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if self.calls == 1:
            response = ChatResponse(
                content=[
                    ToolCallBlock(
                        id=call_id,
                        name="search_web",
                        input=json.dumps({"query": query, "knowledge_gap": "固定批次保护检查"}),
                    )
                    for call_id, query in zip(CALL_IDS, QUERIES, strict=True)
                ],
                is_last=True,
            )
        else:
            sources = [source for block in received for source in payload(block).get("sources", [])]
            answer = (
                "## 结论\n以下引用只用于预置离线流程核对："
                + " ".join(f"[{source['id']}@snippet]" for source in sources)
                + "\n## 局限\n固定模型和证据不代表真实研究质量。"
                if sources
                else "## 结论\n证据不足。本次没有取得可使用的外部来源。"
            )
            response = ChatResponse(content=[TextBlock(text=answer)], is_last=True)
        self.turns.append(
            {
                "model_call": self.calls,
                "received_tool_results": received,
                "returned_tool_calls": [
                    block.model_dump(mode="json")
                    for block in response.content
                    if isinstance(block, ToolCallBlock)
                ],
            }
        )
        return response


def inspect(run, turns, batches, backend_queries, case):
    """Check actual model inputs and per-query evidence, not just sets of tool IDs."""
    limited = case == "rate_limit"
    requests = turns[0]["returned_tool_calls"] if turns else []
    results = turns[1]["received_tool_results"] if len(turns) > 1 else []
    by_id = {block["id"]: payload(block) for block in results}
    delivered = [by_id.get(call_id, {}) for call_id in CALL_IDS]
    expected_attempts = 1 if limited else 2
    expected_statuses = (
        ["error", "blocked_after_stop", "blocked_after_stop"]
        if limited
        else ["ok", "ok", "blocked_after_stop"]
    )
    reason = "search_error" if limited else "search_budget"
    counts = execution_summary(run)
    metrics = run["metrics"]
    trace = run["trace"]
    expected_counts = {
        "tool_invocations": 3,
        "search_attempts": expected_attempts,
        "page_read_attempts": 0,
        "pages_read": 0,
        "blocked_requests": 3 - expected_attempts,
    }
    checks = {
        "two_model_calls": len(turns) == metrics.get("fixture_model_calls") == 2,
        "three_distinct_requests_in_first_response": [block["id"] for block in requests]
        == list(CALL_IDS)
        and all(block["name"] == "search_web" for block in requests)
        and [json.loads(block["input"])["query"] for block in requests] == list(QUERIES),
        "first_model_call_has_no_results": bool(turns) and turns[0]["received_tool_results"] == [],
        "sdk_records_one_sequential_batch": batches
        == [
            {
                "type": "sequential",
                "call_ids": list(CALL_IDS),
                "tool_names": ["search_web"] * 3,
                "concurrency_safe": [False] * 3,
            }
        ],
        "all_results_reach_second_model_by_id": len(results) == len(by_id) == 3
        and set(by_id) == set(CALL_IDS)
        and all(block["name"] == "search_web" for block in results),
        "per_call_business_statuses": [item.get("status") for item in delivered]
        == expected_statuses,
        "per_call_attempt_and_quota_snapshots": [item.get("search_attempts") for item in delivered]
        == ([1, 1, 1] if limited else [1, 2, 2])
        and [item.get("remaining_searches") for item in delivered]
        == ([1, 1, 1] if limited else [1, 0, 0]),
        "stop_snapshots_match_case": [item.get("stop") for item in delivered]
        == ([True] * 3 if limited else [False, True, True])
        and [item.get("stop_reason") for item in delivered]
        == ([reason] * 3 if limited else [None, reason, reason]),
        "per_query_evidence_matches_call_id": all(item.get("sources") == [] for item in delivered)
        if limited
        else [[source["url"] for source in item.get("sources", [])] for item in delivered]
        == [[URLS[0]], [URLS[1]], []],
        "original_rate_limit_category_only_on_first_result": delivered[0].get("error_code")
        == "search_rate_limited"
        and all("error_code" not in item for item in delivered[1:])
        if limited
        else all("error_code" not in item for item in delivered),
        "independent_backend_queries_match_attempts": backend_queries
        == metrics["queries"]
        == list(QUERIES[:expected_attempts]),
        "trace_order_and_backend_flags_match": [event["query"] for event in trace] == list(QUERIES)
        and [event["status"] for event in trace] == expected_statuses
        and [event["backend_attempted"] for event in trace]
        == ([True, False, False] if limited else [True, True, False]),
        "execution_counts_agree": not counts["issues"] and counts["counts"] == expected_counts,
        "all_tool_containers_succeeded": len(results) == 3
        and all(block["state"] == "success" for block in results),
        "completed_keeps_search_stop": metrics["status"]
        == metrics["agent_finished_reason"]
        == "completed"
        and metrics["search_stop_reason"] == metrics["stop_reason"] == reason
        and len(turns) == 2
        and turns[-1]["returned_tool_calls"] == [],
        "evidence_and_warnings_match_case": run["sources"] == run["page_evidence"] == []
        and "证据不足" in run["answer"]
        and any("搜索服务异常" in warning for warning in metrics["warnings"])
        if limited
        else len(run["sources"]) == 2 and run["page_evidence"] == [] and not metrics["warnings"],
        "provider_detail_not_saved": "Synthetic private provider detail"
        not in json.dumps({"run": run, "turns": turns}),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "delivered_observations_by_request": delivered,
    }


async def observe(case):
    if case not in CASES:
        raise ValueError("Unknown fixed batch case")
    backend_queries = []
    batches = []

    def handler(request):
        query = json.loads(request.content)["query"]
        backend_queries.append(query)
        if case == "rate_limit":
            return httpx.Response(429, text="Synthetic private provider detail")
        index = QUERIES.index(query)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": f"Synthetic source {index + 1}",
                        "url": URLS[index],
                        "content": f"[Synthetic fixture] Evidence for batch query {index + 1}.",
                    }
                ]
            },
        )

    original_batch = Agent._batch_tool_calls

    async def record_batches(agent, tool_calls):
        actual = await original_batch(agent, tool_calls)
        for batch in actual:
            tools = [await agent.toolkit.get_tool(call.name) for call in batch.tool_calls]
            batches.append(
                {
                    "type": batch.type,
                    "call_ids": [call.id for call in batch.tool_calls],
                    "tool_names": [call.name for call in batch.tool_calls],
                    "concurrency_safe": [tool.is_concurrency_safe for tool in tools],
                }
            )
        return actual

    model = BatchModel()
    # Observe the SDK's decision, then return its original batch objects. This
    # finite local probe is sequential; the scoped wrapper is restored on exit.
    with patch.object(Agent, "_batch_tool_calls", record_batches):
        run = await run_research(
            "固定离线练习：同一模型响应提出三条搜索，核对预算与失败保护。",
            model,
            TavilySearch("offline-placeholder", httpx.MockTransport(handler)),
            ResearchConfig(max_searches=2, max_page_reads=0),
            mode="offline_fixture",
        )
    run["metrics"]["fixture_model_calls"] = model.calls
    observation = {
        "kind": "native_same_response_batch_observation",
        "case": case,
        "application_version": importlib.metadata.version("agentscope-evidence-researcher"),
        "agentscope_version": importlib.metadata.version("agentscope"),
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "production_source_sha256": run["provenance"]["source_sha256"],
        **inspect(run, model.turns, batches, backend_queries, case),
        "model_turns": model.turns,
        "sdk_batches": batches,
        "backend_queries": backend_queries,
        "execution_summary": execution_summary(run),
        "synthetic_http_requests": len(backend_queries),
        "fixture_model_calls": model.calls,
        "real_model_calls": 0,
        "public_search_requests": 0,
        "actual_mcp_rpcs": 0,
        "note": "Production Agent/Toolkit/policy with fixed BatchModel and MockTransport. Scoped wrapper records and returns the original SDK batches, without changing registration, execution strategy or SDK files. Current search tools are sequential, not a concurrent batch. No .env or real network; not a model quality/obedience score.",
    }
    return run, observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    target = args.output_dir.resolve()
    if not target.is_relative_to((ROOT / "reports").resolve()) or target.exists():
        parser.error("Output must be a new directory inside reports")
    logging.getLogger("as").setLevel(logging.CRITICAL)
    target.mkdir(parents=True, exist_ok=False)
    summary = {}
    for case in CASES:
        run, observation = asyncio.run(observe(case))
        directory = target / case
        directory.mkdir()
        for name, data in (("run.json", run), ("observation.json", observation)):
            with (directory / name).open("x", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.write("\n")
        with (directory / "report.md").open("x", encoding="utf-8") as output:
            output.write(render_report(run))
        summary[case] = {"passed": observation["passed"], **observation["checks"]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(case["passed"] for case in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
