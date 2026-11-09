"""Observe main-answer input using a prescribed read of frozen evidence, without retrieval."""

import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path

from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse
from agentscope.tool import ToolChoice

from as_researcher.core import ResearchConfig, source_id
from as_researcher.evidence import page_observation
from as_researcher.models import ScriptedModel
from as_researcher.researcher import SYSTEM_PROMPT, render_report, run_research

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN = "reports/source-url-v032/live/20261012T125244-f5ba58db/run.json"
SOURCE_SHA = "f8202cb90096e143500e0fdf3ce04566c1ea023566670d40e13b43fd4933e0b7"


def load_source():
    raw = (ROOT / SOURCE_RUN).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA:
        raise ValueError("Saved source changed")
    return json.loads(raw)


class AnswerStageModel(ScriptedModel):
    """First decision is prescribed; at most one delegated, tool-free answer call."""

    def __init__(self, sid, candidate, delegate=None):
        super().__init__()
        self.sid, self.candidate, self.delegate = sid, candidate, delegate
        self.turns = []
        self.provider_responses = delegate.provider_responses if delegate is not None else []
        self.answer_calls = 0
        if delegate is not None:
            self.model = delegate.model
            if hasattr(delegate, "request_events"):
                self.request_events = delegate.request_events

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        if self.calls > 2:
            raise RuntimeError("No second answer request permitted")
        system = messages[0].get_text_content()
        results = [
            block.model_dump(mode="json")
            for message in messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        self.turns.append(
            {"framework_call": self.calls, "system_prompt": system, "tool_results": results}
        )
        if self.calls == 1:
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id="saved-page",
                        name="read_page",
                        input=json.dumps(
                            {
                                "source_id": self.sid,
                                "focus": "asyncio.shield,CancelledError,cancellation",
                            }
                        ),
                    )
                ],
                is_last=True,
            )
        if self.calls != 2:
            raise RuntimeError("No second answer request permitted")
        if self.delegate is None:
            return ChatResponse(content=[TextBlock(text=self.candidate)], is_last=True)
        self.answer_calls += 1
        response = await self.delegate(messages, tools=[], tool_choice=ToolChoice(mode="none"))
        if any(isinstance(block, ToolCallBlock) for block in response.content):
            raise RuntimeError("Answer-stage tool request forbidden")
        return response


async def observe(source, *, maximum=700, delegate=None, candidate=None):
    page = source["page_evidence"][0]
    sid = source_id(page["url"])
    if page["id"] != sid:
        raise ValueError("Saved page identity mismatch")
    answer = source["answer"] if candidate is None else candidate
    model = AnswerStageModel(sid, answer, delegate)
    reads = []

    async def saved_reader(url, limit, focus=""):
        if url != page["url"] or limit < len(page["content"]):
            raise ValueError("Saved evidence cannot be substituted or clipped")
        reads.append({"url": url, "limit": limit, "focus": focus})
        return dict(page)

    async def forbidden_search(*args, **kwargs):
        raise AssertionError("No search permitted")

    run = await run_research(
        source["topic"],
        model,
        forbidden_search,
        ResearchConfig(
            max_searches=0,
            max_page_reads=1,
            chars_per_page=max(5000, len(page["content"])),
            max_agent_iters=2,
            max_answer_chars=maximum,
            total_timeout=60,
        ),
        source_urls=[page["url"]],
        reader=saved_reader,
        mode="live_model_fixture" if delegate is not None else "offline_fixture",
    )
    result = (
        model.turns[1]["tool_results"][0]
        if len(model.turns) > 1 and model.turns[1]["tool_results"]
        else {}
    )
    body = json.loads(result["output"][0]["text"]) if result else {}
    instruction = model.turns[0]["system_prompt"] if model.turns else ""
    expected_page = page_observation(run["page_evidence"][0]) if run["page_evidence"] else {}
    checks = {
        "same_prompt_reaches_both_turns": len(model.turns) == 2
        and instruction == model.turns[1]["system_prompt"],
        "budget_selects_compact_structure": (
            f"不超过 {maximum}" in instruction
            and "结论与对应依据合并" in instruction
            and "报告结构：结论、证据与比较" not in instruction
        )
        if maximum is not None
        else SYSTEM_PROMPT in instruction,
        "saved_body_reaches_answer_stage": result.get("id") == "saved-page"
        and body.get("id") == sid
        and body.get("content") == page["content"]
        and body.get("evidence_is_untrusted") is True
        and all(body.get(key) == value for key, value in expected_page.items()),
        "one_prescribed_read_without_search": len(reads) == 1
        and run["metrics"]["search_attempts"] == 0
        and run["metrics"]["page_read_attempts"] == 1
        and run["metrics"]["pages_read"] == 1,
        "no_revision_or_hard_truncation": run["revision"]["status"] == "disabled"
        and (delegate is not None or run["answer"] == answer),
        "single_answer_request_cap": model.answer_calls <= 1,
    }
    observation = {
        "kind": "main_answer_budget_observation",
        "checks": checks,
        "passed": all(checks.values()),
        "production_source_sha256": run["provenance"]["source_sha256"],
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model_turns": model.turns,
        "prescribed_decisions": 1,
        "delegated_answer_requests": model.answer_calls,
        "saved_reader_calls": reads,
        "network_page_reads": 0,
        "public_search_requests": 0,
        "actual_mcp_rpcs": 0,
        "note": "Prescribed first read decision and replayed saved evidence. Only the answer stage may delegate to a model; not a new live research/retrieval task. Offline default returns the old overlong answer unchanged. Prompt checks do not certify length or meaning.",
    }
    run["metrics"]["fixture_model_calls"] = 1 if delegate is not None else model.calls
    return run, observation


def save(target, run, observation):
    target.mkdir(parents=True, exist_ok=False)
    for name, data in (("run.json", run), ("observation.json", observation)):
        with (target / name).open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    (target / "report.md").write_text(render_report(run), "utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    target = args.output_dir.resolve()
    if target.exists() or not target.is_relative_to((ROOT / "reports").resolve()):
        parser.error("Output must be a new directory within reports")
    logging.getLogger("as").setLevel(logging.CRITICAL)
    run, observation = asyncio.run(observe(load_source()))
    save(target, run, observation)
    print(
        json.dumps(
            {
                "passed": observation["passed"],
                "checks": observation["checks"],
                "answer_chars": run["metrics"]["answer_chars"],
                "answer_within_limit": run["metrics"]["answer_within_limit"],
            }
        )
    )
    return 0 if observation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
