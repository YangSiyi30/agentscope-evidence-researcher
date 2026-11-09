"""Small opt-in live test batch. Never calls APIs without --execute."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from as_researcher.atomic_review import prepare_atomic_review
from as_researcher.review import prepare_review
from as_researcher.worksheet import render_worksheet

CASES = [
    (
        "asyncio_body",
        "请检索 Python 官方文档，解释 asyncio.timeout 与 wait_for 的超时和取消行为。请读取最相关官方页面，focus 使用 asyncio.timeout,wait_for。500字以内，明确未被证据支持的部分。",
        "answer_with_evidence",
    ),
    (
        "mcp_stdio",
        "请根据官方资料解释 MCP 的 stdio 传输方式，以及它与直接调用 Python 函数有什么区别。检索并读取官方来源，focus 使用 stdio。500字以内。",
        "answer_with_evidence",
    ),
    (
        "unanswerable",
        "一家未公布运营数据的公司的私有 Agent 系统，昨天精确调用了多少次模型？请核验能否获得证据，不能推算或编造数字。200字以内。",
        "acknowledge_unknown",
    ),
]

QUALITY_CASES = [
    (
        "fact_stdio",
        "依据 MCP 官方规范说明 stdio 的 stdout 与 stderr 用途。禁止 stdout 混入日志，能否推出禁止写日志文件？搜索后读取一页核验，focus 使用 stdio。仅据取得的证据回答。",
        "bounded_claim_scope",
    ),
    (
        "compare_timeout",
        "依据 Python 官方文档比较 asyncio.timeout 与 wait_for 的取消对象与超时行为。检索后读取最相关页面，focus 使用 asyncio.timeout,wait_for。证据不足处明确说明。",
        "supported_comparison",
    ),
    (
        "current_release",
        f"截至 {datetime.now().astimezone().date()}，AgentScope 最新稳定发行版本是什么？只以官方 release/发行记录核验；区分旧教程与当前版本，找不到则明确未知。读取来源片段。",
        "dated_primary_source",
    ),
    (
        "version_conflict",
        "MCP 2024-11-05 文档的 HTTP+SSE 与 2025-11-25 文档的 Streamable HTTP 看起来不同。这是否构成同一规范的矛盾？检索官方资料并读取关键来源，focus 使用 Streamable HTTP,2024-11-05。明确版本和证据缺口。",
        "version_qualified_conflict",
    ),
    (
        "unknown_private",
        "一家未具名、未披露运营数据的公司的私有 Agent 系统，昨天精确调用了多少次模型？做一次检索核验；不要推算数字，也不要把本次未搜到说成所有公开来源都不存在。",
        "acknowledge_unknown",
    ),
]


def select_cases(cases, case_ids=None, limit=None):
    """Explicit selection never silently includes other paid-model tasks."""
    if case_ids is not None:
        if limit is not None:
            raise ValueError("--case and --limit cannot be combined")
        if not case_ids or len(case_ids) > 5 or len(set(case_ids)) != len(case_ids):
            raise ValueError("Select 1..5 unique case IDs")
        available = {case[0]: case for case in cases}
        unknown = [identity for identity in case_ids if identity not in available]
        if unknown:
            raise ValueError("Unknown case IDs for this suite: " + ", ".join(unknown))
        return [available[identity] for identity in case_ids]
    if limit is not None and not 1 <= limit <= 5:
        raise ValueError("--limit must be 1..5")
    return cases[: limit if limit is not None else 3]


def batch_exit_code(rows, planned_count):
    if len(rows) != planned_count or any(
        row.get("exit_code") not in {0, 3} or row.get("metrics", {}).get("status") != "completed"
        for row in rows
    ):
        return 2
    return 3 if any(row["exit_code"] == 3 for row in rows) else 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--limit",
        type=int,
        choices=range(1, 6),
        help="First N cases; default 3 when --case is absent",
    )
    selection.add_argument(
        "--case",
        dest="case_ids",
        action="append",
        help="Exact ID in selected suite; repeat to select up to five cases",
    )
    parser.add_argument("--suite", choices=["smoke", "quality", "holdout"], default="smoke")
    parser.add_argument(
        "--excerpt-strategy",
        choices=["head", "window", "segments", "bm25", "balanced"],
        default="segments",
    )
    parser.add_argument("--model-retries", type=int, choices=[0, 1], default=0)
    parser.add_argument("--model-max-tokens", type=int, default=1800)
    parser.add_argument("--reader", choices=["none", "http", "browser"], default="http")
    parser.add_argument("--transport", choices=["local", "mcp"], default="mcp")
    parser.add_argument("--chars-per-page", type=int, default=5000)
    parser.add_argument("--revise-answer", action="store_true")
    parser.add_argument("--revision-timeout", type=float, default=30)
    parser.add_argument("--revision-max-tokens", type=int, default=1200)
    args = parser.parse_args(argv)
    if not 64 <= args.model_max_tokens <= 4096:
        parser.error("--model-max-tokens must be between 64 and 4096")
    if not 100 <= args.chars_per_page <= 12000:
        parser.error("--chars-per-page must be between 100 and 12000")
    if not 0 < args.revision_timeout <= 60 or not 64 <= args.revision_max_tokens <= 4096:
        parser.error("revision timeout must be (0,60]; tokens 64..4096")
    root = Path(__file__).resolve().parents[1]
    holdout = root / "eval" / "holdout_v04.json"
    cases = (
        json.loads(holdout.read_text(encoding="utf-8"))["cases"]
        if args.suite == "holdout"
        else QUALITY_CASES
        if args.suite == "quality"
        else CASES
    )
    try:
        cases = select_cases(cases, args.case_ids, args.limit)
    except ValueError as exc:
        parser.error(str(exc))
    plan = {
        "suite": args.suite,
        "cases": cases,
        "selection": {"case_ids": args.case_ids, "limit": args.limit},
        "transport": args.transport,
        "reader": args.reader,
        "chars_per_page": args.chars_per_page,
        "excerpt_strategy": args.excerpt_strategy,
        "model_retries": args.model_retries,
        "model_max_tokens": args.model_max_tokens,
        "revise_answer": args.revise_answer,
        "revision_timeout": args.revision_timeout,
        "revision_max_tokens": args.revision_max_tokens,
        "holdout_sha256": hashlib.sha256(holdout.read_bytes()).hexdigest()
        if args.suite == "holdout"
        else None,
    }
    if not args.execute:
        print(
            "Plan only. --execute runs the selected suite (at most 5 paid-model tasks), each with one search and at most two page reads."
        )
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    batch = (
        root
        / "reports"
        / ("live-eval-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6])
    )
    batch.mkdir(parents=True, exist_ok=False)
    results = []
    model_failures = 0
    (batch / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for case_id, topic, expected in cases:
        target = batch / case_id
        cmd = [
            sys.executable,
            "-m",
            "as_researcher.cli",
            topic,
            "--live",
            "--transport",
            args.transport,
            "--reader",
            args.reader,
            "--chars-per-page",
            str(args.chars_per_page),
            "--pages",
            "2" if case_id == "version_conflict" else "1",
            "--searches",
            "1",
            "--results",
            "3",
            "--timeout",
            "100",
            "--max-agent-iters",
            "6",
            "--max-answer-chars",
            "700",
            "--model-retries",
            str(args.model_retries),
            "--model-max-tokens",
            str(args.model_max_tokens),
            "--excerpt-strategy",
            args.excerpt_strategy,
            "--strict",
            "--output-dir",
            str(target),
        ]
        if args.revise_answer:
            cmd.extend(
                [
                    "--revise-answer",
                    "--revision-timeout",
                    str(args.revision_timeout),
                    "--revision-max-tokens",
                    str(args.revision_max_tokens),
                ]
            )
        try:
            p = subprocess.run(
                cmd,
                cwd=root,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=130 + (args.revision_timeout if args.revise_answer else 0),
            )
            # Child CLI suppresses SDK bodies; record only its structured artifact.
            files = list(target.glob("*/run.json"))
            row = {"case": case_id, "expected_behavior": expected, "exit_code": p.returncode}
            if files:
                result = json.loads(files[-1].read_text(encoding="utf-8"))
                row.update(run_file=str(files[-1].relative_to(root)), metrics=result["metrics"])
                (files[-1].parent / "review.draft.json").write_text(
                    json.dumps(prepare_review(result), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (files[-1].parent / "review.atomic.draft.json").write_text(
                    json.dumps(prepare_atomic_review(result), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (files[-1].parent / "review.worksheet.md").write_text(
                    render_worksheet(result, prepare_atomic_review(result)), encoding="utf-8"
                )
            else:
                row["status"] = "setup_failed_no_artifact"
        except subprocess.TimeoutExpired:
            row = {"case": case_id, "status": "process_deadline"}
        model_failures = model_failures + 1 if batch_exit_code([row], 1) == 2 else 0
        results.append(row)
        (batch / "index.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if model_failures >= 2:
            print(
                "Stopped after two consecutive failed/incomplete tasks; remaining planned cases were not executed.",
                flush=True,
            )
            break
    print("Saved batch:", batch, flush=True)
    print("Exit/ID checks are not quality scores. Manually review refusal and evidence support.")
    return batch_exit_code(results, len(cases))


if __name__ == "__main__":
    raise SystemExit(main())
