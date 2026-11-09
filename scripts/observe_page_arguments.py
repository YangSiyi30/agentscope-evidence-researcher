"""Observe direct page argument guards and native SDK handling using fixed local inputs."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
from pathlib import Path

from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse

from as_researcher.core import ResearchConfig
from as_researcher.execution import execution_summary
from as_researcher.models import ScriptedModel
from as_researcher.pages import FixtureReader
from as_researcher.researcher import ResearchSession, render_report, run_research
from as_researcher.search import FixtureSearch

if __package__:
    from .observe_argument_boundary import payload, text
else:
    from observe_argument_boundary import payload, text

ROOT = Path(__file__).resolve().parents[1]
CASES = ("source_list", "focus_object", "focus_number")
PAGE_IDS = ("page-arg-first", "page-arg-last")


class RecordingReader(FixtureReader):
    def __init__(self):
        self.calls = []

    async def __call__(self, url, max_chars, focus=""):
        self.calls.append({"url": url, "focus": focus})
        return await super().__call__(url, max_chars, focus=focus)


async def observe_direct():
    rows = []
    for field in ("source_id", "focus"):
        for value in (None, 123, ["fixed private marker"], {"fixed private marker": True}):
            reader = RecordingReader()
            session = ResearchSession(
                ResearchConfig(max_searches=1, max_page_reads=1), FixtureSearch(), reader
            )
            await session.search_web("page argument seed")
            source_id = next(iter(session.sources.values())).id
            arguments = {"source_id": source_id, "focus": "", field: value}
            start = len(session.trace)
            try:
                chunk = await session.read_page(**arguments)
                outcome = {"observation": json.loads(chunk.content[0].text)}
            except Exception as error:
                outcome = {"error_type": type(error).__name__}
            after_bad = {
                "trace": session.trace[start:].copy(),
                "page_attempts": session.page_attempts,
                "saved_pages": len(session.page_evidence),
                "read_ids": sorted(session.read_ids),
                "reader_calls": reader.calls.copy(),
            }
            retry = json.loads((await session.read_page(source_id)).content[0].text)
            rows.append(
                {
                    "field": field,
                    "submitted_type": type(value).__name__,
                    **outcome,
                    "after_bad": after_bad,
                    "later_valid_error": retry.get("error"),
                    "later_saved_pages": len(session.page_evidence),
                    "search_stop_reason": session.stop_reason,
                }
            )
    checks = {
        "all_eight_bad_calls_are_structured_errors": len(rows) == 8
        and all(
            row.get("observation", {}).get("error") == "invalid_page_arguments" for row in rows
        ),
        "bad_calls_never_consume_page_resources": all(
            row["after_bad"]["page_attempts"] == row["after_bad"]["saved_pages"] == 0
            and row["after_bad"]["read_ids"] == row["after_bad"]["reader_calls"] == []
            for row in rows
        ),
        "bad_calls_have_safe_trace_and_payload": all(
            len(row["after_bad"]["trace"]) == 1
            and row["after_bad"]["trace"][0]["backend_attempted"] is False
            and row["after_bad"]["trace"][0][row["field"]] == ""
            and isinstance(row.get("observation", {}).get("source_id"), str)
            and "fixed private marker" not in json.dumps(row)
            for row in rows
        ),
        "later_known_source_read_still_succeeds": all(
            row["later_valid_error"] is None
            and row["later_saved_pages"] == 1
            and row["search_stop_reason"] == "search_budget"
            for row in rows
        ),
    }
    return {
        "kind": "direct_page_argument_observation",
        "application_version": importlib.metadata.version("agentscope-deep-researcher-interview"),
        "researcher_file_sha256": hashlib.sha256(
            (ROOT / "src/as_researcher/researcher.py").read_bytes()
        ).hexdigest(),
        "passed": all(checks.values()),
        "checks": checks,
        "rows": rows,
        "note": "New direct calls using synthetic inputs. Pre-fix records may retain malformed synthetic fields to show the defect. Exceptions are saved by class only, not message. No Agent/model/MCP/network/.env in these eight cases.",
    }


class PageArgumentModel(ScriptedModel):
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
            content = [
                ToolCallBlock(
                    id="page-search",
                    name="search_web",
                    input=json.dumps({"query": "page argument seed"}),
                )
            ]
        elif self.calls == 2:
            sources = [source for block in received for source in payload(block).get("sources", [])]
            source_id = sources[0]["id"]
            arguments = {"source_id": source_id}
            if self.case == "source_list":
                arguments["source_id"] = ["fixed invalid source"]
            else:
                arguments["focus"] = (
                    {"fixed": "invalid focus"} if self.case == "focus_object" else 123
                )
            content = [
                ToolCallBlock(id=PAGE_IDS[0], name="read_page", input=json.dumps(arguments)),
                ToolCallBlock(
                    id=PAGE_IDS[1], name="read_page", input=json.dumps({"source_id": source_id})
                ),
            ]
        else:
            pages = [
                payload(block)
                for block in received
                if block["name"] == "read_page"
                and payload(block).get("evidence_kind") == "page_excerpt"
            ]
            content = [
                TextBlock(
                    text=f"## 结论\n固定读页参数检查。[{pages[0]['id']}@page]\n## 局限\n预置动作与正文不代表真实质量。"
                )
            ]
        response = ChatResponse(content=content, is_last=True)
        self.turns.append(
            {
                "model_call": self.calls,
                "received_tool_results": received,
                "returned_tool_calls": [
                    block.model_dump(mode="json")
                    for block in content
                    if isinstance(block, ToolCallBlock)
                ],
            }
        )
        return response


def inspect_native(run, turns, calls, case):
    repaired = case == "focus_number"
    results = (
        [block for block in turns[2]["received_tool_results"] if block["name"] == "read_page"]
        if len(turns) > 2
        else []
    )
    by_id = {block["id"]: block for block in results}
    first = payload(by_id[PAGE_IDS[0]]) if PAGE_IDS[0] in by_id else {}
    last = payload(by_id[PAGE_IDS[1]]) if PAGE_IDS[1] in by_id else {}
    summary = execution_summary(run)
    checks = {
        "three_model_calls_three_requests": len(turns)
        == run["metrics"].get("fixture_model_calls")
        == 3
        and [len(turn["returned_tool_calls"]) for turn in turns] == [1, 2, 0],
        "both_page_results_reach_third_model_by_id": len(results) == len(by_id) == 2
        and set(by_id) == set(PAGE_IDS),
        "first_result_matches_sdk_layer": first.get("evidence_kind") == "page_excerpt"
        if repaired
        else PAGE_IDS[0] in by_id
        and by_id[PAGE_IDS[0]]["state"] == "error"
        and "Input validation failed" in text(by_id[PAGE_IDS[0]]),
        "last_result_matches_actual_read_history": last.get("error") == "already_attempted"
        if repaired
        else last.get("evidence_kind") == "page_excerpt" and "error" not in last,
        "reader_only_called_once_with_string_focus": len(calls) == 1
        and calls[0]["focus"] == ("123" if repaired else ""),
        "application_trace_separates_sdk_rejection": [event["status"] for event in run["trace"]]
        == (["ok", "ok", "error"] if repaired else ["ok", "ok"]),
        "execution_counts_agree": not summary["issues"]
        and summary["counts"]
        == {
            "tool_invocations": 3 if repaired else 2,
            "search_attempts": 1,
            "page_read_attempts": 1,
            "pages_read": 1,
            "blocked_requests": int(repaired),
        },
        "known_source_body_and_completion_preserved": len(run["sources"])
        == len(run["page_evidence"])
        == 1
        and run["sources"][0]["id"] == run["page_evidence"][0]["id"]
        and run["metrics"]["status"] == run["metrics"]["agent_finished_reason"] == "completed"
        and run["metrics"]["search_stop_reason"] == "search_budget",
    }
    return {"passed": all(checks.values()), "checks": checks}


async def observe_native(case):
    if case not in CASES:
        raise ValueError("Unknown fixed page case")
    model, reader = PageArgumentModel(case), RecordingReader()
    run = await run_research(
        "固定离线读页参数练习：核对SDK拒绝/修复与同批合法读取。",
        model,
        FixtureSearch(),
        ResearchConfig(max_searches=1, max_page_reads=1),
        reader=reader,
        mode="offline_fixture",
    )
    run["metrics"]["fixture_model_calls"] = model.calls
    observation = {
        "kind": "native_page_argument_observation",
        "case": case,
        "application_version": importlib.metadata.version("agentscope-deep-researcher-interview"),
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "production_source_sha256": run["provenance"]["source_sha256"],
        **inspect_native(run, model.turns, reader.calls, case),
        "model_turns": model.turns,
        "reader_calls": reader.calls,
        "execution_summary": execution_summary(run),
        "real_model_calls": 0,
        "public_search_requests": 0,
        "actual_mcp_rpcs": 0,
        "note": "Native Agent/Toolkit with fixed model, FixtureSearch and FixtureReader. SDK invalid types stay outside the application body; numeric focus repair is preserved. No real pages/model/network/configuration or factual quality score.",
    }
    return run, observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    target = parser.parse_args().output_dir.resolve()
    if not target.is_relative_to((ROOT / "reports").resolve()) or target.exists():
        parser.error("Output must be a new directory inside reports")
    logging.getLogger("as").setLevel(logging.CRITICAL)
    target.mkdir(parents=True, exist_ok=False)
    direct = asyncio.run(observe_direct())
    with (target / "direct.json").open("x", encoding="utf-8") as output:
        json.dump(direct, output, ensure_ascii=False, indent=2)
        output.write("\n")
    summary = {"direct": direct["passed"]}
    for case in CASES:
        run, observation = asyncio.run(observe_native(case))
        directory = target / case
        directory.mkdir()
        for name, data in (("run.json", run), ("observation.json", observation)):
            with (directory / name).open("x", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.write("\n")
        with (directory / "report.md").open("x", encoding="utf-8") as output:
            output.write(render_report(run))
        summary[case] = observation["passed"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
