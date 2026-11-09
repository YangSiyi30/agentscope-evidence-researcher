"""Only for cancellation tests. No network, model or production tool exposure."""

import asyncio

from mcp.server.fastmcp import FastMCP

server = FastMCP("slow-test-fixture", log_level="CRITICAL")


@server.tool()
async def search(query: str, limit: int = 1) -> dict:
    await asyncio.sleep(60)
    return {"sources": []}


@server.tool()
async def read_public_page(url: str, max_chars: int = 100, focus: str = "") -> dict:
    return {"content": "cancel fixture ready"}


if __name__ == "__main__":
    server.run(transport="stdio")
