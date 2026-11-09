"""Exercise real stdio child exit and cleanup; no model, network or production exit tool."""

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import mcp.client.stdio as stdio
from agentscope.mcp import MCPClient, StdioMCPConfig

from as_researcher.mcp_bridge import MCPBackends


async def run_probe(scenario: str = "exit") -> dict:
    if scenario not in {"exit", "cancel"}:
        raise ValueError("Unknown fault fixture")
    root = Path(__file__).resolve().parents[1]
    remote = MCPBackends("fixture", "fixture")
    # Only this diagnostic replaces the fixed server; no runtime CLI extension.
    remote.client = MCPClient(
        name="research",
        is_stateful=True,
        execution_timeout=3,
        mcp_config=StdioMCPConfig(
            command=sys.executable,
            args=[
                str(
                    root
                    / "tests/fixtures"
                    / ("exiting_mcp_server.py" if scenario == "exit" else "slow_mcp_server.py")
                )
            ],
            cwd=str(root),
            env={"PYTHONIOENCODING": "utf-8"},
        ),
    )
    create = stdio._create_platform_compatible_process
    children = []

    async def capture_child(**kwargs):
        child = await create(**kwargs)
        children.append(child)
        return child

    result = {
        "mode": "real_stdio_fault_fixture",
        "scenario": scenario,
        "ready": False,
        "closed_cleanly": False,
    }
    started = time.perf_counter()
    # Test-only hook for the actual process handle. Pinned MCP SDK contract.
    with patch.object(stdio, "_create_platform_compatible_process", capture_child):
        try:
            async with asyncio.timeout(10):
                async with remote:
                    ready = await remote.read("https://example.org/not-fetched", 100)
                    result["ready"] = ready["content"] == f"{scenario} fixture ready"
                    try:
                        if scenario == "exit":
                            await remote.search("terminate this fixture", 1)
                        else:
                            # Cancel the pending call, without changing the SDK's
                            # connect/close task or enumerating system processes.
                            async with asyncio.timeout(0.1):
                                await remote.search("slow fixture", 1)
                    except Exception as exc:
                        result["call_error_type"] = type(exc).__name__
                result["closed_cleanly"] = True
        except Exception as exc:
            result["lifecycle_error_type"] = type(exc).__name__
        finally:
            exits = []
            for child in children:
                try:
                    code = await asyncio.wait_for(child.wait(), timeout=2)
                except TimeoutError:
                    # The exact child created by this probe only; never enumerate
                    # or terminate another user's/server process.
                    result["forced_cleanup"] = True
                    child.kill()
                    code = await asyncio.wait_for(child.wait(), timeout=2)
                exits.append({"pid": child.pid, "exit_code": code})
    result.update(
        children=exits,
        calls=remote.calls,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    result["passed"] = (
        result["ready"]
        and result["closed_cleanly"]
        and len(exits) == 1
        and (
            exits[0]["exit_code"] == 23 if scenario == "exit" else exits[0]["exit_code"] is not None
        )
        and not result.get("forced_cleanup")
        and len(remote.calls) == 2
        and remote.calls[-1]["status"] == ("error" if scenario == "exit" else "cancelled")
        and remote.calls[-1]["error_layer"]
        == ("transport_or_payload" if scenario == "exit" else "transport_or_caller")
    )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["exit", "cancel"], default="exit")
    args = parser.parse_args()
    logging.getLogger("as").setLevel(logging.CRITICAL)
    result = asyncio.run(run_probe(args.scenario))
    target = Path("reports") / (
        "mcp-"
        + args.scenario
        + "-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        + "-"
        + uuid4().hex[:6]
        + ".json"
    )
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(target), **result}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
