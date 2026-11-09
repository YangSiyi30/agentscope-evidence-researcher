"""Observe one native AgentScope failure loop with fixed model/search substitutes."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
from pathlib import Path

import httpx
from agentscope.message import ToolCallBlock, ToolResultBlock

from as_researcher.core import ResearchConfig
from as_researcher.models import ScriptedModel
from as_researcher.researcher import render_report, run_research
from as_researcher.search import TavilySearch

ROOT = Path(__file__).resolve().parents[1]


class RecordingModel(ScriptedModel):
    """Record tool messages at the model boundary; keep the existing scripted choices."""

    def __init__(self):
        super().__init__()
        self.turns = []

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        received = [
            block.model_dump(mode="json")
            for message in messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        response = await super()._call_api(
            model_name, messages, tools=tools, tool_choice=tool_choice, **kwargs
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


async def observe():
    """Use the production loop/adapter, with no service configuration or network."""
    requests = []

    def handler(request):
        # Only a fixed counter is saved, never request headers or provider detail.
        requests.append("synthetic_http_429")
        return httpx.Response(429, text="Synthetic private provider detail")

    model = RecordingModel()
    config = ResearchConfig(max_searches=2, max_page_reads=0)
    result = await run_research(
        "离线控制流练习：第一次搜索限流后，保存证据不足的回答。",
        model,
        TavilySearch("offline-placeholder", httpx.MockTransport(handler)),
        config,
        mode="offline_fixture",
    )
    result["metrics"]["fixture_model_calls"] = model.calls
    # The second call proves delivery: this is copied from messages that the
    # actual AgentScope loop passed to the model, not reconstructed from Trace.
    turn_results = [t["received_tool_results"] for t in model.turns]
    delivered = turn_results[1] if len(turn_results) > 1 else []
    first_calls = model.turns[0]["returned_tool_calls"] if model.turns else []
    payload = (
        json.loads("".join(block["text"] for block in delivered[0]["output"]))
        if len(delivered) == 1 and isinstance(delivered[0]["output"], list)
        else {}
    )
    metrics = result["metrics"]
    checks = {
        "two_model_calls": model.calls == 2,
        "first_call_has_no_tool_result": bool(turn_results) and turn_results[0] == [],
        "matching_tool_call_and_result": len(first_calls) == len(delivered) == 1
        and first_calls[0]["id"] == delivered[0]["id"]
        and first_calls[0]["name"] == delivered[0]["name"] == "search_web",
        "tool_container_succeeded": len(delivered) == 1 and delivered[0]["state"] == "success",
        "business_rate_limit_delivered": payload.get("status") == "error"
        and payload.get("error_code") == "search_rate_limited",
        "stopped_with_remaining_budget": payload.get("stop") is True
        and payload.get("stop_reason") == "search_error"
        and payload.get("search_attempts") == payload.get("remaining_searches") == 1,
        "no_second_tool_request": len(model.turns) == 2
        and model.turns[1]["returned_tool_calls"] == [],
        "agent_completed_but_search_failed": metrics["status"] == "completed"
        and metrics["agent_finished_reason"] == "completed"
        and metrics["search_stop_reason"] == metrics["stop_reason"] == "search_error",
        "one_backend_attempt_no_evidence": len(requests)
        == metrics["search_attempts"]
        == metrics["tool_invocations"]
        == 1
        and metrics["page_read_attempts"] == metrics["pages_read"] == 0
        and result["sources"] == result["page_evidence"] == [],
        "insufficient_answer_and_warnings": "证据不足" in result["answer"]
        and any("搜索服务异常" in w for w in metrics["warnings"])
        and any("未取得外部证据" in w for w in metrics["warnings"]),
        "provider_detail_not_saved": "Synthetic private provider detail"
        not in json.dumps({"run": result, "turns": model.turns}),
    }
    observation = {
        "kind": "scripted_model_native_failure_loop",
        "application_version": importlib.metadata.version("agentscope-deep-researcher-interview"),
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "production_source_sha256": result["provenance"]["source_sha256"],
        "passed": all(checks.values()),
        "checks": checks,
        "model_turns": model.turns,
        "delivered_business_observation": payload,
        "synthetic_http_requests": len(requests),
        "fixture_model_calls": model.calls,
        "real_model_calls": 0,
        "public_search_requests": 0,
        "actual_mcp_rpcs": 0,
        "note": "Actual AgentScope Agent/Toolkit loop with RecordingModel (a ScriptedModel subclass) and MockTransport search. Tool results are recorded at the model input boundary, not inferred from Trace. Does not load .env. The checker can pass while research has search_error; this is not a successful live research or model quality score.",
    }
    return result, observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    target = args.output_dir.resolve()
    if not target.is_relative_to((ROOT / "reports").resolve()) or target.exists():
        parser.error("Output must be a new directory inside reports")
    logging.getLogger("as").setLevel(logging.CRITICAL)
    result, observation = asyncio.run(observe())
    target.mkdir(parents=True, exist_ok=False)
    for name, data in (("run.json", result), ("observation.json", observation)):
        with (target / name).open("x", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
    with (target / "report.md").open("x", encoding="utf-8") as output:
        output.write(render_report(result))
    print(
        json.dumps(
            {"output": str(target), "passed": observation["passed"], **observation["checks"]}
        )
    )
    return 0 if observation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
