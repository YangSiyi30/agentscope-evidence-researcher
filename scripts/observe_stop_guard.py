"""Observe code-enforced search stopping and independent page reads; fixed local cases only."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
from pathlib import Path

import httpx
from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse

from as_researcher.core import ResearchConfig
from as_researcher.execution import execution_summary
from as_researcher.models import ScriptedModel
from as_researcher.pages import FixtureReader
from as_researcher.researcher import render_report, run_research
from as_researcher.search import TavilySearch

ROOT = Path(__file__).resolve().parents[1]
CASES = ("rate_limit", "search_budget")
QUERIES = ("agent memory design", "agent memory evaluation")


def payload(block):
    """Decode a copied tool result, never manufacture one from the run's Trace."""
    return json.loads("".join(item["text"] for item in block["output"]))


class ContinueAfterStopModel(ScriptedModel):
    """Deliberately request one extra search, then finish or read a known source."""

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
        observations = [payload(block) for block in received if block["name"] == "search_web"]
        sources = [source for observation in observations for source in observation["sources"]]
        if self.calls <= 2:
            response = ChatResponse(
                content=[
                    ToolCallBlock(
                        id="stop-first" if self.calls == 1 else "stop-extra",
                        name="search_web",
                        input=json.dumps(
                            {"query": QUERIES[self.calls - 1], "knowledge_gap": "离线保护验证"}
                        ),
                    )
                ],
                is_last=True,
            )
        elif self.case == "search_budget" and self.calls == 3:
            response = ChatResponse(
                content=[
                    ToolCallBlock(
                        id="stop-page",
                        name="read_page",
                        input=json.dumps(
                            {"source_id": sources[0]["id"] if sources else "SRC-00000000"}
                        ),
                    )
                ],
                is_last=True,
            )
        else:
            answer = (
                f"## 结论\n预置正文区分检索相关性与事实支持。[{sources[0]['id']}@page]\n"
                "## 局限\n这是固定离线工具保护练习，不代表真实研究质量。"
                if self.case == "search_budget" and sources
                else "## 结论\n证据不足。本次未取得可用来源，后续搜索被程序拦截。"
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


class CountingReader(FixtureReader):
    def __init__(self):
        self.calls = 0

    async def __call__(self, url, max_chars, focus=""):
        self.calls += 1
        return await super().__call__(url, max_chars, focus=focus)


def inspect(run, turns, backend_calls, page_calls, case):
    """Require input-boundary messages, matching IDs and independent backend counters."""
    wants_page = case == "search_budget"
    expected_calls = [("stop-first", "search_web"), ("stop-extra", "search_web")]
    if wants_page:
        expected_calls.append(("stop-page", "read_page"))
    delivered = []
    matching = len(turns) == len(expected_calls) + 1
    for index, (call_id, name) in enumerate(expected_calls):
        requests = turns[index]["returned_tool_calls"] if index < len(turns) else []
        results = turns[index + 1]["received_tool_results"] if index + 1 < len(turns) else []
        matches = [block for block in results if block["id"] == call_id and block["name"] == name]
        matching = matching and len(requests) == len(matches) == 1
        matching = matching and requests[0]["id"] == call_id and requests[0]["name"] == name
        delivered.append(payload(matches[0]) if len(matches) == 1 else {})
    first, refused = delivered[:2]
    metrics = run["metrics"]
    trace = run["trace"]
    counts = execution_summary(run)
    expected_counts = {
        "tool_invocations": 3 if wants_page else 2,
        "search_attempts": 1,
        "page_read_attempts": int(wants_page),
        "pages_read": int(wants_page),
        "blocked_requests": 1,
    }
    reason = "search_budget" if wants_page else "search_error"
    checks = {
        "matching_requests_and_next_model_results": matching,
        "first_turn_has_no_results": bool(turns) and turns[0]["received_tool_results"] == [],
        "distinct_extra_query_after_stop": len(turns) > 1
        and len(turns[1]["returned_tool_calls"]) == 1
        and json.loads(turns[1]["returned_tool_calls"][0]["input"])["query"] == QUERIES[1]
        and first.get("stop") is True,
        "first_result_explains_stop": first.get("stop_reason") == reason
        and first.get("status") == ("ok" if wants_page else "error")
        and (wants_page or first.get("error_code") == "search_rate_limited"),
        "refusal_reaches_third_model_call": refused.get("status") == "blocked_after_stop"
        and refused.get("stop") is True
        and refused.get("stop_reason") == reason
        and refused.get("sources") == [],
        "refusal_keeps_attempt_and_quota": first.get("search_attempts")
        == refused.get("search_attempts")
        == 1
        and first.get("remaining_searches")
        == refused.get("remaining_searches")
        == int(not wants_page),
        "only_one_search_backend_call": backend_calls == metrics["search_attempts"] == 1,
        "refused_trace_never_entered_backend": len(trace) >= 2
        and trace[0]["backend_attempted"] is True
        and trace[1]["backend_attempted"] is False
        and trace[1]["status"] == "blocked_after_stop",
        "execution_counts_agree": not counts["issues"] and counts["counts"] == expected_counts,
        "blocked_query_not_in_attempted_queries": metrics["queries"] == [QUERIES[0]],
        "agent_finishes_without_erasing_stop": metrics["status"]
        == metrics["agent_finished_reason"]
        == "completed"
        and metrics["search_stop_reason"] == metrics["stop_reason"] == reason
        and bool(turns)
        and turns[-1]["returned_tool_calls"] == [],
        "all_tool_containers_succeeded": matching
        and all(
            block["state"] == "success" for turn in turns for block in turn["received_tool_results"]
        ),
        "independent_page_policy": page_calls == int(wants_page)
        and (
            len(delivered) == 3
            and len(run["sources"]) == len(run["page_evidence"]) == 1
            and "error" not in delivered[2]
            and delivered[2].get("evidence_kind") == "page_excerpt"
            and delivered[2].get("source_id") == run["sources"][0]["id"]
            and run["page_evidence"][0]["id"] == run["sources"][0]["id"]
            and not metrics["warnings"]
            if wants_page
            else run["sources"] == run["page_evidence"] == []
            and "证据不足" in run["answer"]
            and any("搜索服务异常" in warning for warning in metrics["warnings"])
        ),
        "provider_detail_not_saved": "Synthetic private provider detail"
        not in json.dumps({"run": run, "turns": turns}),
    }
    return {"passed": all(checks.values()), "checks": checks, "delivered_observations": delivered}


async def observe(case):
    if case not in CASES:
        raise ValueError("Unknown fixed stop-guard case")
    requests = []

    def handler(request):
        requests.append("synthetic_search")
        if case == "rate_limit":
            return httpx.Response(429, text="Synthetic private provider detail")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Synthetic source",
                        "url": "https://example.org/memory",
                        "content": "[Synthetic snippet] Retrieval and evidence support require separate checks.",
                    }
                ]
            },
        )

    model = ContinueAfterStopModel(case)
    reader = CountingReader() if case == "search_budget" else None
    config = ResearchConfig(max_searches=1 if reader else 2, max_page_reads=int(reader is not None))
    run = await run_research(
        "固定离线练习：搜索停止后故意再请求搜索，检查程序保护和工具反馈。",
        model,
        TavilySearch("offline-placeholder", httpx.MockTransport(handler)),
        config,
        reader=reader,
        mode="offline_fixture",
    )
    run["metrics"]["fixture_model_calls"] = model.calls
    observation = {
        "kind": "native_stop_guard_observation",
        "case": case,
        "application_version": importlib.metadata.version("agentscope-evidence-researcher"),
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "production_source_sha256": run["provenance"]["source_sha256"],
        **inspect(run, model.turns, len(requests), reader.calls if reader else 0, case),
        "model_turns": model.turns,
        "execution_summary": execution_summary(run),
        "synthetic_http_requests": len(requests),
        "fixture_page_calls": reader.calls if reader else 0,
        "fixture_model_calls": model.calls,
        "real_model_calls": 0,
        "public_search_requests": 0,
        "actual_mcp_rpcs": 0,
        "note": "Native production Agent/Toolkit and policy with a deliberately noncompliant fixed model, MockTransport and optional FixtureReader. No .env or network services. Search stop does not globally disable known-source page reads. Passing checks are not real model obedience or research quality scores.",
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
