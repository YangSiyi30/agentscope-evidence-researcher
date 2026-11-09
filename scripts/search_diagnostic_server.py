"""Diagnostic-only stdio wrapper: production tools plus a bounded observation RPC."""

import logging

from mcp.types import ToolAnnotations

from as_researcher.mcp_server import server
from scripts.search_diagnostics import HttpObservation

observation = HttpObservation()


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def http_observation() -> dict:
    """Read diagnostic HTTP counters only; never perform network requests."""
    return observation.snapshot()


def main():
    for name in ("as", "ddgs", "primp", "mcp", "httpx"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    with observation:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
