"""Probe real public-page or MCP access without calling a model."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from as_researcher.mcp_bridge import MCPBackends
from as_researcher.pages import PageReader, browser_text


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reader", choices=["http", "browser"], default="http")
    parser.add_argument("--transport", choices=["local", "mcp"], default="mcp")
    parser.add_argument("--browser-fixture", action="store_true")
    parser.add_argument("--page", choices=["python", "sqlite", "shield"], default="python")
    parser.add_argument("--budget", type=int, default=5000)
    args = parser.parse_args()
    if not 100 <= args.budget <= 12000:
        parser.error("--budget must be 100..12000")
    result = {"reader": args.reader, "transport": args.transport}
    try:
        if args.browser_fixture:
            text = await browser_text(
                '<html><body><nav>noise</nav><main>Browser extraction works.</main><script>document.body.innerHTML="UNSAFE"</script><img src="http://127.0.0.1:9/test"></body></html>'
            )
            result["passed"] = (
                "Browser extraction works." in text and "UNSAFE" not in text and "noise" not in text
            )
        else:
            url, focus = (
                ("https://www.sqlite.org/datatype3.html", "VARCHAR(255)")
                if args.page == "sqlite"
                else (
                    "https://docs.python.org/3/library/asyncio-task.html",
                    "asyncio.timeout,wait_for",
                )
            )
            if args.page == "shield":
                focus = "asyncio.shield,CancelledError"
            if args.transport == "mcp":
                async with MCPBackends("duckduckgo", args.reader) as remote:
                    try:
                        text = await remote.read(url, args.budget, focus=focus)
                    finally:
                        result["mcp_calls"] = remote.calls
            else:
                text = await PageReader(args.reader)(url, args.budget, focus=focus)
            page = text if isinstance(text, dict) else {"content": text}
            text = page["content"]
            result.update(
                passed=bool(text) and len(text) <= args.budget,
                url=url,
                chars=len(text),
                excerpt=text[:1200],
                strategy=page.get("strategy"),
                selector_version=page.get("selector_version"),
                selection_warnings=page.get("selection_warnings", []),
                segment_count=len(page.get("segments", [])),
                budget=args.budget,
                code_selection=page.get("code_selection"),
            )
            if args.page == "shield":
                result["code_and_condition_preserved"] = all(
                    part in text
                    for part in (
                        "create_task(something())",
                        "res = await something()",
                        "CancelledError",
                    )
                )
                result["passed"] = result["passed"] and result["code_and_condition_preserved"]
            if args.page == "sqlite":
                result["table_header_value_preserved"] = "Resulting Affinity: TEXT" in text
                result["passed"] = (
                    result["passed"]
                    and result["table_header_value_preserved"]
                    and len(text) <= args.budget
                )
    except Exception as exc:
        result.update(passed=False, error_type=type(exc).__name__)
    path = Path("reports") / (
        "tool-probe-"
        + args.transport
        + "-"
        + args.reader
        + ("-fixture" if args.browser_fixture else "")
        + "-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        + ".json"
    )
    path.parent.mkdir(exist_ok=True)
    result["output"] = str(path)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
