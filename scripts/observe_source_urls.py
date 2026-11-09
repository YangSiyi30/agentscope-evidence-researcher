"""Observe supplied-source input and native read-only AgentScope flow; fixtures only."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
from pathlib import Path

from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse

from as_researcher.core import ResearchConfig, source_id
from as_researcher.execution import execution_summary
from as_researcher.models import ScriptedModel
from as_researcher.pages import FixtureReader
from as_researcher.researcher import render_report, run_research
from as_researcher.source_inputs import SOURCE_CONTEXT_MARKER

ROOT = Path(__file__).resolve().parents[1]
URL = "https://example.org/user-document"


class SuppliedSourceModel(ScriptedModel):
    def __init__(self):
        super().__init__()
        self.turns = []

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        registered = []
        received = []
        for message in messages:
            for block in message.content:
                if isinstance(block, TextBlock) and SOURCE_CONTEXT_MARKER in block.text:
                    registered = json.loads(block.text.rsplit(SOURCE_CONTEXT_MARKER, 1)[1])[
                        "provided_sources"
                    ]
                if isinstance(block, ToolResultBlock):
                    received.append(block.model_dump(mode="json"))
        sid = registered[0]["id"] if registered else source_id(URL)
        if self.calls == 1:
            content = [
                ToolCallBlock(
                    id="supplied-page",
                    name="read_page",
                    input=json.dumps({"source_id": sid, "focus": "relevance"}),
                )
            ]
        else:
            content = [
                TextBlock(
                    text=(
                        f"## 结论\n这是指定来源的固定流程演示。[{sid}@page]\n"
                        "## 局限\n固定证据不代表真实网页或模型质量。"
                    )
                )
            ]
        self.turns.append(
            {
                "model_call": self.calls,
                "registered_sources_received": registered,
                "received_tool_results": received,
                "returned_tool_calls": [
                    b.model_dump(mode="json") for b in content if isinstance(b, ToolCallBlock)
                ],
            }
        )
        return ChatResponse(content=content, is_last=True)


def inspect_source_flow(run, turns):
    received = (
        [b for b in turns[1]["received_tool_results"] if b["name"] == "read_page"]
        if len(turns) > 1
        else []
    )
    body = json.loads(received[0]["output"][0]["text"]) if len(received) == 1 else {}
    sources, pages = run["sources"], run["page_evidence"]
    summary = execution_summary(run)
    checks = {
        "source_catalog_reaches_first_model": bool(turns)
        and turns[0]["registered_sources_received"] == [{"id": source_id(URL), "url": URL}],
        "url_is_not_a_search_snippet": len(sources) == 1
        and sources[0]["content"] == ""
        and sources[0]["evidence_kind"] == "source_url"
        and sources[0]["citation"] is None,
        "actual_page_result_reaches_second_model": len(received) == 1
        and received[0]["id"] == "supplied-page"
        and body.get("id") == source_id(URL)
        and body.get("evidence_kind") == "page_excerpt"
        and "error" not in body,
        "one_real_fixture_read_without_search": summary["counts"]
        == {
            "tool_invocations": 1,
            "search_attempts": 0,
            "page_read_attempts": 1,
            "pages_read": 1,
            "blocked_requests": 0,
        }
        and not summary["issues"]
        and len(pages) == 1
        and pages[0]["url"] == URL,
        "two_model_calls_and_valid_page_citation": len(turns) == 2
        and run["metrics"].get("fixture_model_calls") == 2
        and run["metrics"]["evidence_levels"]["contract_valid"]
        and run["metrics"]["status"] == "completed",
    }
    return {"passed": all(checks.values()), "checks": checks}


async def observe_source_flow(*, baseline=False, reader=None):
    async def no_search(*args, **kwargs):
        raise AssertionError("Source-only task must never call a search backend")

    model = SuppliedSourceModel()
    config = ResearchConfig(max_searches=1 if baseline else 0, max_page_reads=1)
    options = {} if baseline else {"source_urls": [URL]}
    run = await run_research(
        f"请阅读这份已知来源：{URL}。",
        model,
        no_search,
        config,
        reader=reader or FixtureReader(),
        mode="offline_fixture",
        **options,
    )
    run["metrics"]["fixture_model_calls"] = model.calls
    observation = {
        "kind": "supplied_source_native_observation",
        "application_version": importlib.metadata.version("agentscope-deep-researcher-interview"),
        "baseline": baseline,
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "production_source_sha256": run["provenance"]["source_sha256"],
        **inspect_source_flow(run, model.turns),
        "model_turns": model.turns,
        "execution_summary": execution_summary(run),
        "real_model_calls": 0,
        "public_search_requests": 0,
        "network_pages": 0 if reader is None else None,
        "actual_mcp_rpcs": 0 if reader is None else None,
        "note": "Baseline sends only topic URL and cannot read its unregistered ID. Current flow passes a bounded source catalog, not a fabricated tool result or snippet. Default model/reader are fixtures. For a caller-supplied reader, network/MCP counts are unknown here; inspect that transport separately.",
    }
    return run, observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    target = args.output_dir.resolve()
    if not target.is_relative_to((ROOT / "reports").resolve()) or target.exists():
        parser.error("Output must be a new directory inside reports")
    logging.getLogger("as").setLevel(logging.CRITICAL)
    target.mkdir(parents=True, exist_ok=False)
    run, observation = asyncio.run(observe_source_flow(baseline=args.baseline))
    for name, data in (("run.json", run), ("observation.json", observation)):
        with (target / name).open("x", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    (target / "report.md").write_text(render_report(run), encoding="utf-8")
    print(json.dumps({"passed": observation["passed"], "checks": observation["checks"]}))
    return 0 if observation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
