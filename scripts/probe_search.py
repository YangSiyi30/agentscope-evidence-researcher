"""Frozen same-query component diagnostic. Plan by default; no model or .env."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from as_researcher.search import DDGSearch, search_error_code

# Executed as a file by the documented CLI, so resolve the sibling helper
# without changing installed packages or accepting a user-provided module.
if __package__:
    from .search_diagnostics import HttpObservation, diagnostic_remote, read_observation
else:
    from search_diagnostics import HttpObservation, diagnostic_remote, read_observation

ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTIC_FILES = ("scripts/search_diagnostics.py", "scripts/search_diagnostic_server.py")


def source_hash(root=ROOT):
    return hashlib.sha256(
        b"".join(
            p.name.encode() + b"\0" + p.read_bytes()
            for p in sorted((root / "src/as_researcher").glob("*.py"))
        )
    ).hexdigest()


def load_plan(path: Path):
    raw = path.read_bytes()
    plan = json.loads(raw)
    if (
        type(plan.get("schema_version")) is not int
        or plan["schema_version"] != 2
        or plan.get("kind") != "search_transport_diagnostic"
    ):
        raise ValueError("Unsupported diagnostic plan")
    if plan.get("provider") != "duckduckgo" or plan.get("transports") != ["local", "mcp"]:
        raise ValueError("This probe requires DuckDuckGo local/MCP only")
    query, limit, timeout = plan["query"], plan["result_limit"], plan["search_timeout"]
    if not isinstance(query, str) or not query.strip() or len(query) > 400:
        raise ValueError("Invalid query")
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Invalid result limit")
    if type(timeout) not in (int, float) or not 0 < timeout <= 20:
        raise ValueError("Invalid search timeout")
    for key, actual in (
        ("source_sha256", source_hash()),
        ("probe_sha256", hashlib.sha256(Path(__file__).read_bytes()).hexdigest()),
        ("ddgs_version", importlib.metadata.version("ddgs")),
    ):
        if plan.get(key) != actual:
            raise ValueError("Frozen diagnostic mismatch: " + key)
    expected = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in DIAGNOSTIC_FILES
    }
    if plan.get("diagnostic_files_sha256") != expected:
        raise ValueError("Frozen diagnostic mismatch: diagnostic_files_sha256")
    return raw, plan


async def local_search(plan, observation):
    with observation:
        return await DDGSearch()(plan["query"], plan["result_limit"])


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


async def execute(raw, plan, transport, output_root):
    if transport not in plan["transports"]:
        raise ValueError("Unknown transport")
    folder = output_root / (
        "search-diagnostic-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    )
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "plan.json").write_bytes(raw)
    result = {
        "kind": "search_transport_diagnostic_result",
        "schema_version": 2,
        "transport": transport,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "started",
        "phase": "setup",
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "query": plan["query"],
        "provider": plan["provider"],
        "result_limit": plan["result_limit"],
        "source_sha256": plan["source_sha256"],
        "ddgs_version": plan["ddgs_version"],
        "model_calls": 0,
        "page_reads": 0,
        "logical_search_attempts": 0,
        "http_observation": None,
        "http_observation_scope": "local diagnostic process only"
        if transport == "local"
        else "diagnostic stdio subprocess only; production search handler unchanged",
        "mcp_calls": [],
        "note": "One component search, no model/.env/page. No application retries or provider fallback. Status/result count does not isolate a root cause. Raw messages, response bodies and headers are not saved.",
        "http_observation_note": "Snapshot of ddgs.HttpClient.request boundaries, not network redirect hops. response_bytes is len(response.content), not bytes on the wire. Events are in completion order, capped at 16 with dropped count. In-flight or later work may be absent at cancellation; no complete-network-capture claim. MCP search and observation RPCs are separate.",
    }
    save(folder / "result.json", result)
    started = time.perf_counter()
    remote = None
    observation = HttpObservation() if transport == "local" else None
    try:
        async with asyncio.timeout(
            40
        ):  # Includes MCP startup/cleanup; thread shutdown may outlive cancellation.
            if transport == "local":
                result.update(phase="search", logical_search_attempts=1)
                save(folder / "result.json", result)
                sources = await asyncio.wait_for(
                    local_search(plan, observation), plan["search_timeout"]
                )
            else:
                remote = diagnostic_remote()
                async with remote:
                    result.update(phase="search", logical_search_attempts=1)
                    save(folder / "result.json", result)
                    try:
                        sources = await asyncio.wait_for(
                            remote.search(plan["query"], plan["result_limit"]),
                            plan["search_timeout"],
                        )
                    finally:
                        if asyncio.current_task().cancelling():
                            result["http_observation_error"] = "not_collected_due_to_cancellation"
                        else:
                            try:
                                result["http_observation"] = await asyncio.wait_for(
                                    read_observation(remote), 2
                                )
                            except Exception:
                                result["http_observation_error"] = "snapshot_unavailable_or_invalid"
                    result["phase"] = "cleanup"
        result.update(
            status="ok",
            phase="finished",
            result_count=len(sources),
            source_ids=[source.id for source in sources],
        )
    except Exception as exc:
        result.update(status="error", error_code=search_error_code(exc))
    except asyncio.CancelledError:
        result.update(status="cancelled", error_code="caller_cancelled")
        raise
    finally:
        if observation is not None:
            result["http_observation"] = observation.snapshot()
        if remote is not None:
            result["mcp_calls"] = [
                {
                    key: event[key]
                    for key in (
                        "tool",
                        "call_id",
                        "status",
                        "duration_ms",
                        "error_layer",
                        "search_error_code",
                    )
                    if key in event
                }
                for event in remote.calls
            ]
            result["mcp_call_counts"] = {
                "search": sum(e["tool"] == "mcp__research__search" for e in result["mcp_calls"]),
                "http_observation": sum(
                    e["tool"] == "mcp__research__http_observation" for e in result["mcp_calls"]
                ),
            }
        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        save(folder / "result.json", result)
    print(json.dumps({"output": str(folder), **result}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=ROOT / "eval/search_diagnostic_v016.json")
    parser.add_argument("--transport", choices=["local", "mcp"])
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.execute and args.transport is None:
        parser.error("--execute requires one --transport; never run both implicitly")
    try:
        raw, plan = load_plan(args.plan)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error("Invalid diagnostic plan: " + type(exc).__name__)
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "plan_only",
                    "plan": plan,
                    "selected_transport": args.transport,
                    "max_searches_this_execution": 1,
                    "model_calls": 0,
                    "page_reads": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    for name in ("as", "ddgs", "primp", "mcp", "httpx"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    return asyncio.run(execute(raw, plan, args.transport, ROOT / "reports"))


if __name__ == "__main__":
    raise SystemExit(main())
