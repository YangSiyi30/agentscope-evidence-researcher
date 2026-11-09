"""Low-cost CLI: offline by default; live mode requires an explicit flag."""

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from .core import ResearchConfig
from .mcp_bridge import MCPBackends
from .models import SCENARIOS, ScriptedModel, live_model
from .pages import FixtureReader, PageReader
from .researcher import render_report, run_research
from .search import FixtureSearch, make_search
from .source_inputs import normalize_source_urls


def save_run(result: dict, root: Path) -> Path:
    """Each execution gets its own directory; never overwrite an earlier run."""
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    target = root / run_id
    target.mkdir(parents=True, exist_ok=False)
    (target / "report.md").write_text(render_report(result), encoding="utf-8")
    (target / "run.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    revision = result.get("revision", {})
    for key, name in (
        ("original_answer", "answer.original.md"),
        ("candidate_answer", "answer.candidate.md"),
    ):
        if isinstance(revision.get(key), str):
            (target / name).write_text(revision[key], encoding="utf-8")
    return target


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentScope evidence-first research demo")
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Chinese terminal wizard (offline by default)",
    )
    parser.add_argument("topic", nargs="?", default="如何评测 Agent 的记忆能力？")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use real model and selected search APIs (may incur cost)",
    )
    parser.add_argument(
        "--scenario", choices=SCENARIOS, default="normal", help="Offline fixture scenario"
    )
    parser.add_argument("--searches", type=int, default=3)
    parser.add_argument(
        "--search-provider", choices=["auto", "tavily", "duckduckgo", "fixture"], default="auto"
    )
    parser.add_argument("--transport", choices=["local", "mcp"], default="local")
    parser.add_argument("--reader", choices=["none", "http", "browser"], default="none")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument(
        "--source-url",
        action="append",
        default=[],
        help="Register up to 3 supplied public URLs without inventing snippets; requires --reader and --pages > 0. Use --searches 0 for source-only reading.",
    )
    parser.add_argument(
        "--excerpt-strategy",
        choices=["head", "window", "segments", "bm25", "balanced"],
        default="segments",
    )
    parser.add_argument("--max-answer-chars", type=int, default=None)
    parser.add_argument("--answer-style", choices=["standard", "compact"], default="standard")
    parser.add_argument(
        "--model-thinking",
        choices=["provider-default", "disabled"],
        default="provider-default",
        help="Main model only: explicit non-thinking mode for verified DeepSeek official models",
    )
    parser.add_argument(
        "--revise-answer",
        action="store_true",
        help="Opt in to one tool-free shortening call for a completed overlong answer; requires --max-answer-chars",
    )
    parser.add_argument("--revision-timeout", type=float, default=30)
    parser.add_argument("--revision-max-tokens", type=int, default=1200)
    parser.add_argument(
        "--revision-thinking",
        choices=["provider-default", "disabled"],
        default="provider-default",
        help="Revision only: explicitly disable thinking on verified DeepSeek official models",
    )
    parser.add_argument(
        "--model-max-tokens",
        type=int,
        default=1800,
        help="Per-response provider output token cap, 64..4096; distinct from answer characters",
    )
    parser.add_argument(
        "--browser-channel", choices=["msedge", "chrome", "chromium"], default="msedge"
    )
    parser.add_argument("--results", type=int, default=3)
    parser.add_argument("--chars-per-source", type=int, default=1500)
    parser.add_argument("--chars-per-page", type=int, default=5000)
    parser.add_argument("--max-agent-iters", type=int, default=10)
    parser.add_argument(
        "--model-retries",
        type=int,
        choices=[0, 1, 2],
        default=0,
        help="Bounded model transport retries; SDK retries stay disabled",
    )
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument(
        "--no-duplicate-stop", action="store_true", help="Ablation: disable query duplicate guard"
    )
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero on missing/unknown citation IDs or evidence",
    )
    return parser


def execute_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[dict, Path]:
    """Run one parsed task and save it; shared by scripts and the terminal wizard."""
    try:
        source_urls = normalize_source_urls(args.source_url)
        if source_urls and (args.reader == "none" or args.pages <= 0):
            raise ValueError("--source-url requires --reader http/browser and --pages > 0")
    except ValueError as exc:
        parser.error(str(exc))
    if args.revision_thinking != "provider-default" and not args.live:
        parser.error("--revision-thinking disabled requires --live and --revise-answer")
    if args.model_thinking != "provider-default" and not args.live:
        parser.error("--model-thinking disabled requires --live")
    # Silence third-party payload/error logging; sanitized failures are in run.json.
    logging.getLogger("as").setLevel(logging.CRITICAL)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.CRITICAL)
    load_dotenv(Path.cwd() / ".env", override=False, encoding="utf-8-sig")
    try:
        config = ResearchConfig(
            max_searches=args.searches,
            results_per_search=args.results,
            chars_per_source=args.chars_per_source,
            chars_per_page=args.chars_per_page,
            max_agent_iters=args.max_agent_iters,
            total_timeout=args.timeout,
            early_stop_duplicates=not args.no_duplicate_stop,
            max_page_reads=args.pages,
            max_answer_chars=args.max_answer_chars,
            answer_style=args.answer_style,
            revise_answer=args.revise_answer,
            revision_timeout=args.revision_timeout,
            revision_max_tokens=args.revision_max_tokens,
            revision_thinking=args.revision_thinking,
        )
        if args.live:
            provider = args.search_provider
            if provider == "auto":
                provider = "tavily" if os.environ.get("TAVILY_API_KEY") else "duckduckgo"
            backend = make_search(provider)
            model = live_model(
                max_retries=args.model_retries,
                max_tokens=args.model_max_tokens,
                thinking_mode=args.model_thinking,
            )
        else:
            model, backend = ScriptedModel(args.scenario), FixtureSearch(args.scenario)

        async def execute():
            revision_model = None
            try:
                if config.revise_answer:
                    revision_model = (
                        live_model(
                            max_retries=0,
                            max_tokens=config.revision_max_tokens,
                            thinking_mode=config.revision_thinking,
                        )
                        if args.live
                        else ScriptedModel()
                    )
                async with AsyncExitStack() as stack:
                    reader_mode = (
                        args.reader
                        if args.live
                        else (
                            "fixture"
                            if args.scenario == "tool_chain" or args.reader != "none"
                            else "none"
                        )
                    )
                    channel = "" if args.browser_channel == "chromium" else args.browser_channel
                    page_reader = None
                    effective_backend = backend
                    if args.transport == "mcp":
                        remote = await stack.enter_async_context(
                            MCPBackends(
                                provider if args.live else "fixture",
                                reader_mode,
                                channel,
                                scenario=args.scenario,
                                strategy=args.excerpt_strategy,
                            )
                        )
                        effective_backend = remote.search
                        if reader_mode != "none":
                            page_reader = remote.read
                    elif reader_mode != "none":
                        page_reader = (
                            FixtureReader()
                            if reader_mode == "fixture"
                            else PageReader(reader_mode, channel, strategy=args.excerpt_strategy)
                        )
                    result = await run_research(
                        args.topic,
                        model,
                        effective_backend,
                        config,
                        reader=page_reader,
                        revision_model=revision_model,
                        source_urls=source_urls,
                        mode=("live_model_fixture" if provider == "fixture" else "live")
                        if args.live
                        else "offline_fixture",
                    )
                    result.update(
                        tool_transport=args.transport,
                        reader=reader_mode,
                        excerpt_strategy=args.excerpt_strategy,
                    )
                    if args.transport == "mcp":
                        result["mcp"] = {
                            "discovered_tools": remote.discovered_tools,
                            "calls": remote.calls,
                        }
                    return result
            finally:
                if args.live:
                    await model.client.close()
                    if revision_model is not None:
                        await revision_model.client.close()

        result = asyncio.run(execute())
        result["search_provider"] = provider if args.live else "fixture"
        result["model_retry_budget"] = args.model_retries if args.live else 0
        result["model_max_tokens"] = args.model_max_tokens if args.live else None
        result["model_thinking"] = args.model_thinking if args.live else None
        if not args.live:
            result["fixture_scenario"] = args.scenario
            result["metrics"]["fixture_model_calls"] = model.calls
        target = save_run(result, args.output_dir)
    except (ValueError, OSError) as exc:
        # Configuration errors are safe to explain; filesystem errors may include
        # paths, but model/search exception bodies are not surfaced here.
        parser.error(str(exc))
    except Exception as exc:
        parser.exit(
            2,
            f"Tool/model setup failed: {type(exc).__name__}. Check installation and configuration.\n",
        )
    return result, target


def result_exit_code(result: dict, strict: bool) -> int:
    metrics = result["metrics"]
    if metrics["status"] != "completed" or metrics["stop_reason"] == "search_error":
        return 2
    if strict and (metrics["warnings"] or metrics["unique_sources"] == 0):
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = create_parser()
    args = parser.parse_args(argv)
    if args.interactive or (not argv and sys.stdin.isatty() and sys.stdout.isatty()):
        # Only the output root is shared with the wizard; do not silently discard
        # a supplied topic or flags such as --live/--searches.
        remaining = iter(argv)
        for item in remaining:
            if item in {"--interactive", "-i"} or item.startswith("--output-dir="):
                continue
            if item == "--output-dir":
                next(remaining)
                continue
            parser.error(
                "--interactive only accepts --output-dir; choose task settings in the menu"
            )
        from .terminal import run_terminal

        return run_terminal(args.output_dir)
    result, target = execute_args(args, parser)
    print(
        json.dumps(
            {"output": str(target.resolve()), **result["metrics"]}, ensure_ascii=False, indent=2
        )
    )
    return result_exit_code(result, args.strict)


if __name__ == "__main__":
    sys.exit(main())
