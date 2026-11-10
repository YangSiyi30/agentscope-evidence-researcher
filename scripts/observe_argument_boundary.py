"""Observe SDK argument handling and application type guards; fixed local cases only."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
from pathlib import Path

from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse

from as_researcher.core import ResearchConfig, Source
from as_researcher.execution import execution_summary
from as_researcher.models import ScriptedModel
from as_researcher.researcher import render_report, run_research

ROOT = Path(__file__).resolve().parents[1]
CASES = ("missing_query", "query_list", "gap_object", "number_repair")
FIRST, LAST = "argument first query", "argument last query"
IDS = ("arg-first", "arg-bad", "arg-last")


def text(block):
    output = block["output"]
    return output if isinstance(output, str) else "".join(item["text"] for item in output)


def payload(block):
    return json.loads(text(block)) if block["state"] == "success" else {}


def middle_arguments(case):
    return {
        "missing_query": {"knowledge_gap": "fixed missing required field"},
        "query_list": {"query": ["fixed unrepaired query"]},
        "gap_object": {"query": "unused bad gap query", "knowledge_gap": {"fixed": "bad gap"}},
        "number_repair": {"query": 123},
    }[case]


class ArgumentModel(ScriptedModel):
    def __init__(self, case):
        super().__init__()
        self.case = case
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
            arguments = ({"query": FIRST}, middle_arguments(self.case), {"query": LAST})
            response = ChatResponse(
                content=[
                    ToolCallBlock(id=call_id, name="search_web", input=json.dumps(args))
                    for call_id, args in zip(IDS, arguments, strict=True)
                ],
                is_last=True,
            )
        else:
            sources = [source for block in received for source in payload(block).get("sources", [])]
            response = ChatResponse(
                content=[
                    TextBlock(
                        text="## 结论\n固定离线参数边界检查："
                        + " ".join(f"[{source['id']}@snippet]" for source in sources)
                        + "\n## 局限\n预置动作和证据不代表真实研究质量。"
                    )
                ],
                is_last=True,
            )
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


def inspect(run, turns, backend_queries, case):
    results = turns[1]["received_tool_results"] if len(turns) > 1 else []
    by_id = {block["id"]: block for block in results}
    delivered = [payload(by_id[call_id]) if call_id in by_id else {} for call_id in IDS]
    repaired = case == "number_repair"
    rejected = not repaired
    expected_queries = [FIRST, "123"] if repaired else [FIRST, LAST]
    expected_states = ["success", "error" if rejected else "success", "success"]
    expected_statuses = ["ok", "ok"] if rejected else ["ok", "ok", "blocked_after_stop"]
    expected_counts = {
        "tool_invocations": 2 if rejected else 3,
        "search_attempts": 2,
        "page_read_attempts": 0,
        "pages_read": 0,
        "blocked_requests": 0 if rejected else 1,
    }
    trace = run["trace"]
    counts = execution_summary(run)
    metrics = run["metrics"]
    submitted = turns[0]["returned_tool_calls"] if turns else []
    checks = {
        "two_model_calls_three_requests": len(turns) == metrics.get("fixture_model_calls") == 2
        and [block["id"] for block in submitted] == list(IDS),
        "all_three_results_delivered_by_id": len(results) == len(by_id) == 3
        and set(by_id) == set(IDS)
        and all(block["name"] == "search_web" for block in results),
        "outer_states_match_layer": [
            by_id[call_id]["state"] if call_id in by_id else None for call_id in IDS
        ]
        == expected_states,
        "middle_request_keeps_original_arguments": len(submitted) == 3
        and json.loads(submitted[1]["input"]) == middle_arguments(case),
        "middle_result_explains_rejection_or_repair": "Input validation failed"
        in text(by_id[IDS[1]])
        if rejected and IDS[1] in by_id
        else delivered[1].get("status") == "ok",
        "independent_backend_queries_are_correct": backend_queries
        == metrics["queries"]
        == expected_queries,
        "application_trace_explains_layer": [event["status"] for event in trace]
        == expected_statuses
        and [event["backend_attempted"] for event in trace]
        == ([True, True] if rejected else [True, True, False]),
        "application_trace_keeps_string_fields": all(
            isinstance(event["query"], str) and isinstance(event["knowledge_gap"], str)
            for event in trace
        ),
        "execution_counts_agree": not counts["issues"] and counts["counts"] == expected_counts,
        "later_request_follows_real_budget": delivered[2].get("status")
        == ("blocked_after_stop" if repaired else "ok")
        and delivered[2].get("stop_reason") == "search_budget",
        "two_saved_sources_and_completed_loop": len(run["sources"]) == 2
        and run["page_evidence"] == []
        and metrics["status"] == metrics["agent_finished_reason"] == "completed"
        and metrics["search_stop_reason"] == metrics["stop_reason"] == "search_budget"
        and len(turns) == 2
        and turns[-1]["returned_tool_calls"] == [],
    }
    return {"passed": all(checks.values()), "checks": checks, "business_observations": delivered}


async def observe(case):
    if case not in CASES:
        raise ValueError("Unknown fixed argument case")
    backend_queries = []

    async def backend(query, limit):
        backend_queries.append(query)
        suffix = hashlib.sha256(query.encode()).hexdigest()[:8]
        return [
            Source(
                "Synthetic argument source",
                f"https://example.org/argument/{suffix}",
                "[Synthetic fixture] Argument boundary evidence.",
            )
        ]

    model = ArgumentModel(case)
    run = await run_research(
        "固定离线练习：同批中间请求参数不合格时，核对其他请求和结果记录。",
        model,
        backend,
        ResearchConfig(max_searches=2, max_page_reads=0),
        mode="offline_fixture",
    )
    run["metrics"]["fixture_model_calls"] = model.calls
    observation = {
        "kind": "native_argument_boundary_observation",
        "case": case,
        "application_version": importlib.metadata.version("agentscope-evidence-researcher"),
        "agentscope_version": importlib.metadata.version("agentscope"),
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "production_source_sha256": run["provenance"]["source_sha256"],
        **inspect(run, model.turns, backend_queries, case),
        "model_turns": model.turns,
        "backend_queries": backend_queries,
        "execution_summary": execution_summary(run),
        "fixture_model_calls": model.calls,
        "real_model_calls": 0,
        "public_search_requests": 0,
        "actual_mcp_rpcs": 0,
        "note": "Actual production Agent/Toolkit/Session with fixed arguments and a local synthetic search function. Agent repairs then validates against schema: required/type failures stay outside the application body; numeric query repair can proceed. Direct ResearchSession calls bypass that Agent check and are verified separately. Controlled fixture only, no .env/network; not a real model quality score.",
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
