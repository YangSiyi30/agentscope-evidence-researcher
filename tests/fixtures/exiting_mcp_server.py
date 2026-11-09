"""Dedicated fault fixture. Never register these tools in the application server."""

import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("exit-fixture", log_level="ERROR")


@server.tool()
async def search(query: str, limit: int = 1) -> dict:
    """Deliberately terminate only this test child without sending a response."""
    os._exit(23)


@server.tool()
async def read_public_page(url: str, max_chars: int = 100, focus: str = "") -> dict:
    """Return fixed readiness text without accessing any URL."""
    return {"content": "exit fixture ready"}


if __name__ == "__main__":
    server.run(transport="stdio")
