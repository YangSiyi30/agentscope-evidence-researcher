"""Trusted local MCP server. No model keys or user-configurable shell commands."""

import asyncio
import os
from dataclasses import asdict

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .pages import FixtureReader, PageReader, page_error_code
from .search import FixtureSearch, SearchBackendError, make_search

server = FastMCP("research-tools", log_level="ERROR")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def search(query: str, limit: int = 3) -> dict:
    """Search public evidence; return title, URL and snippet."""
    if not query.strip() or len(query) > 400 or not 1 <= limit <= 5:
        return {"error": "invalid_search_arguments"}
    try:
        backend = make_search(os.environ.get("RESEARCH_SEARCH_PROVIDER", "fixture"))
        if os.environ.get("RESEARCH_SEARCH_PROVIDER", "fixture") == "fixture":
            backend = FixtureSearch(os.environ.get("RESEARCH_FIXTURE_SCENARIO", "normal"))
        items = await asyncio.wait_for(backend(query, limit), timeout=20)
        return {"sources": [asdict(s) for s in items]}
    except SearchBackendError as exc:
        return {"error": "SearchBackendError", "search_error_code": exc.search_error_code}
    except Exception as exc:
        return {"error": type(exc).__name__}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def read_public_page(url: str, max_chars: int = 5000, focus: str = "") -> dict:
    """Read a public HTML page within a length and time budget."""
    if not 100 <= max_chars <= 12000 or len(focus) > 120:
        return {"error": "invalid_page_budget"}
    mode = os.environ.get("RESEARCH_READER", "http")
    if mode == "none":
        return {"error": "reader_disabled"}
    try:
        reader = (
            FixtureReader()
            if mode == "fixture"
            else PageReader(
                mode,
                os.environ.get("RESEARCH_BROWSER_CHANNEL", "msedge"),
                os.environ.get("RESEARCH_EXCERPT_STRATEGY", "segments"),
            )
        )
        content = await asyncio.wait_for(reader(url, max_chars, focus=focus), timeout=30)
        return {"content": content} if isinstance(content, str) else content
    except Exception as exc:
        return {"error": page_error_code(exc)}


if __name__ == "__main__":
    server.run(transport="stdio")
