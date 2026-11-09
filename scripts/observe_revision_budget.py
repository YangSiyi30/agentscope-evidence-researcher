"""Saved evidence plus mock HTTP only; never load .env or call paid services."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from agentscope.credential import OpenAICredential

from as_researcher.core import ResearchConfig, Source
from as_researcher.models import ObservedOpenAIChatModel
from as_researcher.revision import revise_answer, revision_integrity_errors, revision_length_budget

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN = "reports/source-url-v032/live/20261012T125244-f5ba58db/run.json"
SOURCE_SHA = "f8202cb90096e143500e0fdf3ce04566c1ea023566670d40e13b43fd4933e0b7"


def synthetic_sources():
    return [
        {"id": source.id, "url": source.url, "title": source.title, "content": source.content}
        for source in (
            Source(f"fixture-{i}", f"https://example.org/{i}", "Synthetic snippet.")
            for i in range(6)
        )
    ]


def received_input(request):
    content = request["messages"][-1]["content"]
    # AgentScope serializes UserMsg text as OpenAI content blocks.
    text = content if isinstance(content, str) else "".join(block["text"] for block in content)
    return json.loads(text)


async def exercise(topic, original, sources, pages, maximum, candidate):
    """Real SDK request formatting/response parsing; HTTP is fixed in memory."""
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "fixture-revision",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": candidate},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = ObservedOpenAIChatModel(
            credential=OpenAICredential(api_key="fixture-key", base_url="https://model.invalid/v1"),
            model="fixture",
            stream=False,
            max_retries=0,
            client_kwargs={"http_client": client, "max_retries": 0},
        )
        record = await revise_answer(
            topic,
            original,
            sources,
            pages,
            ResearchConfig(revise_answer=True, max_answer_chars=maximum),
            model,
        )
    return record, requests


async def observe():
    raw = (ROOT / SOURCE_RUN).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA:
        raise ValueError("Saved source changed")
    run = json.loads(raw)
    sid = run["page_evidence"][0]["id"]
    # Hand-written fixture: this is not a new model answer or human gold.
    candidate = (
        f"依据Python 3.13保存片段：调用方取消时，shield中的内部Task继续运行，"
        f"调用方await仍抛出CancelledError；内部任务经其他途径取消，也会取消shield [{sid}@page]。\n"
        f"协程会自动调度为Task；需保留Task引用。文档不推荐完全忽略取消；"
        f"若这样做，示例用try/except CancelledError处理 [{sid}@page]。\n"
        "范围：仅核对保存片段，未核验整页或版本差异。"
    )
    normal, received = await exercise(
        run["topic"], run["answer"], run["sources"], run["page_evidence"], 700, candidate
    )
    sources = synthetic_sources()
    citations = "".join(f"[{s['id']}@snippet]" for s in sources)
    impossible, rejected_requests = await exercise(
        "Synthetic budget contradiction", "说明" + citations, sources, [], 100, citations
    )
    request_input = received_input(received[0])
    checks = {
        "saved_input_budget_delivered": request_input["length_budget"] == normal["length_budget"],
        "saved_evidence_preserved": len(request_input["saved_evidence"]) == 1
        and request_input["saved_evidence"][0]["content"] == run["page_evidence"][0]["content"]
        and request_input["saved_evidence"][0]["citation"] == f"[{sid}@page]",
        "one_tool_free_mock_request": len(received) == 1
        and not received[0].get("tools")
        and received[0]["tool_choice"] == "none",
        "normal_mechanics_only": normal["applied"]
        and normal["semantic_review"] == "required_not_automated",
        "impossible_skips_request": not rejected_requests
        and impossible["attempts"] == 0
        and impossible["errors"] == ["revision_citation_budget_impossible"],
        "records_consistent": not revision_integrity_errors(normal, candidate)
        and not revision_integrity_errors(impossible, impossible["original_answer"]),
    }
    return {
        "source_run": SOURCE_RUN,
        "source_run_sha256": SOURCE_SHA,
        "application_source_sha256": hashlib.sha256(
            b"".join(
                p.name.encode() + b"\0" + p.read_bytes()
                for p in sorted((ROOT / "src/as_researcher").glob("*.py"))
            )
        ).hexdigest(),
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "saved_answer_budget": revision_length_budget(run["answer"], 700),
        "saved_case": {"revision": normal, "received_requests": received},
        "impossible_case": {"revision": impossible, "received_requests": rejected_requests},
        "checks": checks,
        "passed": all(checks.values()),
        "external_model_calls": 0,
        "public_searches": 0,
        "page_reads": 0,
        "note": "Saved live evidence; hand-written candidate and synthetic contradiction; mock HTTP only. No new live effectiveness or semantic quality proof.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    result = asyncio.run(observe())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "checks": result["checks"],
                "saved_answer_budget": result["saved_answer_budget"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
