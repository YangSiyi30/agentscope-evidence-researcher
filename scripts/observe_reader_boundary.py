"""Fixed reader metadata through direct calls and native AgentScope model input."""

import argparse
import asyncio
import copy
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
from as_researcher.researcher import ResearchSession, render_report, run_research
from as_researcher.search import FixtureSearch

if __package__:
    from .observe_argument_boundary import payload
else:
    from observe_argument_boundary import payload

ROOT = Path(__file__).resolve().parents[1]
CASES = ("identity", "controls")
CONTENT = "[Synthetic page] The word error in body text is evidence data, not a tool failure."
CONTROL_KEYS = {
    "error",
    "error_code",
    "status",
    "stop",
    "stop_reason",
    "sources",
    "tool",
    "state",
    "backend_attempted",
    "internal_note",
}
BUDGET_KEYS = {
    "tool_observation_version",
    "search_attempts",
    "page_read_attempts",
    "pages_read",
    "remaining_searches",
    "remaining_page_reads",
    "search_stopped",
    "page_reader_enabled",
    "count_semantics",
}


class MetadataReader:
    """Return synthetic top-level collisions, never a fetched page or real secret."""

    def __init__(self, case):
        self.calls = []
        self.value = {"content": CONTENT, "strategy": "fixture", "is_full_document": False}
        if case in {"identity", "combined"}:
            self.value.update(
                id="SRC-11111111",
                source_id="SRC-00000000",
                url="https://example.org/unrelated",
                citation="[SRC-11111111@snippet]",
                evidence_kind="search_snippet",
                fetched_at="synthetic-reader-time",
                evidence_is_untrusted=False,
            )
        if case in {"controls", "combined"}:
            self.value.update(
                error="synthetic_reader_error",
                error_code="synthetic_reader_code",
                status="error",
                stop=True,
                stop_reason="synthetic_reader_stop",
                sources=[{"id": "SRC-00000000"}],
                tool="synthetic_tool",
                state="error",
                backend_attempted=False,
                internal_note={"synthetic": "not page metadata"},
            )
            self.value.update({key: "synthetic_counter" for key in BUDGET_KEYS})

    async def __call__(self, url, max_chars, focus=""):
        self.calls.append({"url": url, "focus": focus})
        return copy.deepcopy(self.value)


async def observe_direct():
    rows = []
    for case in CASES:
        reader = MetadataReader(case)
        before = copy.deepcopy(reader.value)
        session = ResearchSession(
            ResearchConfig(max_searches=1, max_page_reads=1), FixtureSearch(), reader
        )
        search = json.loads((await session.search_web("reader boundary seed")).content[0].text)
        source = search["sources"][0]
        reply = json.loads((await session.read_page(source["id"])).content[0].text)
        trace = copy.deepcopy(session.trace[-1])
        saved = copy.deepcopy(session.page_evidence.get(source["id"], {}))
        retry = json.loads((await session.read_page(source["id"])).content[0].text)
        checks = {
            "source_identity_owned_by_application": reply.get("source_id") == source["id"]
            and all(
                page.get("id") == source["id"]
                and page.get("url") == source["url"]
                and page.get("citation") == f"[{source['id']}@page]"
                and page.get("evidence_kind") == "page_excerpt"
                and page.get("fetched_at") != before.get("fetched_at")
                for page in (reply, saved)
            ),
            "tool_evidence_remains_untrusted": reply.get("evidence_is_untrusted") is True,
            "valid_body_remains_success": "error" not in reply
            and "error" not in saved
            and trace["status"] == "ok"
            and trace["backend_attempted"] is True,
            "reader_control_fields_not_saved_or_forwarded": not (CONTROL_KEYS & reply.keys())
            and not (
                (CONTROL_KEYS | BUDGET_KEYS | {"source_id", "evidence_is_untrusted"}) & saved.keys()
            ),
            "usage_and_duplicate_policy_preserved": reply["search_attempts"]
            == reply["page_read_attempts"]
            == reply["pages_read"]
            == 1
            and trace["budget_after"]["pages_read"] == 1
            and retry.get("error") == "already_attempted"
            and session.page_attempts == len(session.page_evidence) == len(reader.calls) == 1,
            "content_and_supported_metadata_preserved": all(
                page.get("content") == CONTENT
                and page.get("strategy") == "fixture"
                and page.get("is_full_document") is False
                for page in (reply, saved)
            ),
            "reader_result_not_mutated": before == reader.value,
        }
        rows.append(
            {
                "case": case,
                "checks": checks,
                "passed": all(checks.values()),
                "source": source,
                "reader_return": before,
                "page_reply": reply,
                "saved_page": saved,
                "page_trace": trace,
                "retry_reply": retry,
                "reader_calls": reader.calls,
            }
        )
    return {
        "kind": "direct_reader_boundary_observation",
        "application_version": importlib.metadata.version("agentscope-deep-researcher-interview"),
        "researcher_file_sha256": hashlib.sha256(
            (ROOT / "src/as_researcher/researcher.py").read_bytes()
        ).hexdigest(),
        "passed": all(row["passed"] for row in rows),
        "rows": rows,
        "note": "Synthetic dictionaries, not real pages, model calls, MCP RPC or configuration.",
    }


class ReaderBoundaryModel(ScriptedModel):
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
            content = [
                ToolCallBlock(
                    id="metadata-search",
                    name="search_web",
                    input=json.dumps({"query": "reader boundary seed"}),
                )
            ]
        else:
            source = next(
                payload(block)["sources"][0] for block in received if block["name"] == "search_web"
            )
            if self.calls == 2:
                content = [
                    ToolCallBlock(
                        id="metadata-page",
                        name="read_page",
                        input=json.dumps({"source_id": source["id"]}),
                    )
                ]
            else:
                content = [
                    TextBlock(
                        text=(
                            f"## 结论\n这是固定reader字段边界演示。[{source['id']}@page]\n"
                            "## 局限\n预置回答不证明真实模型质量。"
                        )
                    )
                ]
        self.turns.append(
            {
                "model_call": self.calls,
                "received_tool_results": received,
                "returned_tool_calls": [
                    b.model_dump(mode="json") for b in content if isinstance(b, ToolCallBlock)
                ],
            }
        )
        return ChatResponse(content=content, is_last=True)


def inspect_native(run, turns, calls):
    results = (
        [b for b in turns[2]["received_tool_results"] if b["name"] == "read_page"]
        if len(turns) > 2
        else []
    )
    observation = payload(results[0]) if len(results) == 1 else {}
    source = run["sources"][0] if len(run["sources"]) == 1 else {}
    saved = run["page_evidence"][0] if len(run["page_evidence"]) == 1 else {}
    summary = execution_summary(run)
    checks = {
        "three_model_calls_and_two_requests": len(turns)
        == run["metrics"].get("fixture_model_calls")
        == 3
        and [len(t["returned_tool_calls"]) for t in turns] == [1, 1, 0],
        "actual_page_result_reaches_third_model_by_id": len(results) == 1
        and results[0]["id"] == "metadata-page"
        and results[0]["state"] == "success",
        "model_and_saved_source_identity_agree": bool(source)
        and bool(saved)
        and observation.get("source_id") == observation.get("id") == saved.get("id") == source["id"]
        and observation.get("url") == saved.get("url") == source["url"]
        and observation.get("citation") == saved.get("citation") == f"[{source['id']}@page]",
        "model_observation_is_untrusted_and_successful": observation.get("evidence_is_untrusted")
        is True
        and "error" not in observation
        and not (CONTROL_KEYS & observation.keys()),
        "saved_body_has_no_reader_controls": saved.get("content")
        == observation.get("content")
        == CONTENT
        and not (
            (CONTROL_KEYS | BUDGET_KEYS | {"source_id", "evidence_is_untrusted"}) & saved.keys()
        ),
        "trace_reader_and_counts_agree": [e["status"] for e in run["trace"]] == ["ok", "ok"]
        and calls == [{"url": source.get("url"), "focus": ""}]
        and not summary["issues"]
        and summary["counts"]
        == {
            "tool_invocations": 2,
            "search_attempts": 1,
            "page_read_attempts": 1,
            "pages_read": 1,
            "blocked_requests": 0,
        }
        and observation.get("page_read_attempts") == observation.get("pages_read") == 1,
        "completion_and_available_page_reference": run["metrics"]["status"] == "completed"
        and run["metrics"]["search_stop_reason"] == "search_budget"
        and run["metrics"]["evidence_levels"]["contract_valid"],
    }
    return {"passed": all(checks.values()), "checks": checks}


async def observe_native():
    model, reader = ReaderBoundaryModel(), MetadataReader("combined")
    run = await run_research(
        "固定reader返回字段练习：保存正文、Trace与交给模型的观察应一致。",
        model,
        FixtureSearch(),
        ResearchConfig(max_searches=1, max_page_reads=1),
        reader=reader,
        mode="offline_fixture",
    )
    run["metrics"]["fixture_model_calls"] = model.calls
    observation = {
        "kind": "native_reader_boundary_observation",
        "application_version": importlib.metadata.version("agentscope-deep-researcher-interview"),
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "production_source_sha256": run["provenance"]["source_sha256"],
        **inspect_native(run, model.turns, reader.calls),
        "model_turns": model.turns,
        "reader_calls": reader.calls,
        "reader_return": reader.value,
        "execution_summary": execution_summary(run),
        "real_model_calls": 0,
        "public_search_requests": 0,
        "actual_mcp_rpcs": 0,
        "note": "Native Agent/Toolkit, fixed model and metadata reader. No real model/network/page/browser/configuration; no prompt-injection or factual-quality claim.",
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
    with (target / "direct.json").open("x", encoding="utf-8") as f:
        json.dump(direct, f, ensure_ascii=False, indent=2)
        f.write("\n")
    run, observation = asyncio.run(observe_native())
    native = target / "native"
    native.mkdir()
    for name, data in (("run.json", run), ("observation.json", observation)):
        with (native / name).open("x", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    (native / "report.md").write_text(render_report(run), encoding="utf-8")
    print(json.dumps({"direct": direct["passed"], "native": observation["passed"]}, indent=2))
    return 0 if direct["passed"] and observation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
