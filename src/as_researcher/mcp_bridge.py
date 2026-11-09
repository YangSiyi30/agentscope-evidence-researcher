"""AgentScope MCPClient adapter beneath the same application policy boundary."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from agentscope.mcp import MCPClient, StdioMCPConfig
from agentscope.message import TextBlock

from .core import Source
from .pages import PAGE_READ_ERROR_CODES, PageReadError
from .search import SEARCH_ERROR_CODES

# Only project-owned codes and known backend exception labels may enter the
# saved RPC trace. An arbitrary payload.error is not a safe public message.
MCP_BACKEND_ERROR_CODES = PAGE_READ_ERROR_CODES | {
    "invalid_search_arguments",
    "invalid_page_budget",
    "reader_disabled",
    "SearchBackendError",
    "HTTPStatusError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "TimeoutError",
    "ValueError",
    "OSError",
    "Error",
}


class MCPBackendError(RuntimeError):
    def __init__(self, message: str, *, search_error_code=None):
        code = (
            message
            if isinstance(message, str) and message in MCP_BACKEND_ERROR_CODES
            else "backend_error"
        )
        super().__init__(code)
        self.search_error_code = (
            search_error_code
            if isinstance(search_error_code, str) and search_error_code in SEARCH_ERROR_CODES
            else None
        )


class MCPBackends:
    """Connect/close in the same asyncio task (MCP uses AnyIO cancel scopes)."""

    def __init__(
        self,
        provider: str,
        reader: str,
        channel: str = "msedge",
        scenario: str = "normal",
        strategy: str = "segments",
    ):
        env = {
            "PYTHONIOENCODING": "utf-8",
            "RESEARCH_SEARCH_PROVIDER": provider,
            "RESEARCH_READER": reader,
            "RESEARCH_BROWSER_CHANNEL": channel,
            "RESEARCH_FIXTURE_SCENARIO": scenario,
            "RESEARCH_EXCERPT_STRATEGY": strategy,
        }
        if provider == "tavily" and os.environ.get("TAVILY_API_KEY"):
            env["TAVILY_API_KEY"] = os.environ["TAVILY_API_KEY"]
        # The MCP SDK adds only its documented minimal OS environment. Do not
        # pass os.environ wholesale: the server does not need the model key.
        self.client = MCPClient(
            name="research",
            is_stateful=True,
            execution_timeout=35,
            mcp_config=StdioMCPConfig(
                command=sys.executable,
                args=["-m", "as_researcher.mcp_server"],
                cwd=str(Path(__file__).resolve().parents[2]),
                env=env,
            ),
        )
        self.mode = reader
        self.calls = []

    async def __aenter__(self):
        await self.client.connect()
        try:
            raw = await self.client.list_raw_tools()
            self.discovered_tools = [t.name for t in raw]
            self.search_tool = await self.client.get_tool("search")
            self.page_tool = await self.client.get_tool("read_public_page")
        except BaseException:
            await self.client.close()
            raise
        return self

    async def __aexit__(self, *args):
        await self.client.close()

    async def _call(self, tool, **arguments):
        started = time.perf_counter()
        event = {"tool": tool.name, "call_id": len(self.calls) + 1, "status": "running"}
        self.calls.append(event)
        try:
            chunk = await tool.call(**arguments)
            payload = json.loads("".join(b.text for b in chunk.content if isinstance(b, TextBlock)))
            if not isinstance(payload, dict):
                raise ValueError("Invalid MCP payload")
            if "error" in payload:
                error = MCPBackendError(
                    payload["error"], search_error_code=payload.get("search_error_code")
                )
                event.update(status="error", error_code=str(error), error_layer="backend")
                if error.search_error_code:
                    event["search_error_code"] = error.search_error_code
                raise error
            event["status"] = "ok"
            return payload
        except asyncio.CancelledError:
            event.update(status="cancelled", error_layer="transport_or_caller")
            raise
        except Exception as exc:
            if event["status"] == "running":
                event.update(
                    status="error",
                    error_code=type(exc).__name__,
                    error_layer="transport_or_payload",
                )
            raise
        finally:
            event["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)

    async def search(self, query: str, limit: int) -> list[Source]:
        payload = await self._call(self.search_tool, query=query, limit=limit)
        return [Source(**item) for item in payload["sources"]]

    async def read(self, url: str, max_chars: int, focus: str = "") -> dict:
        try:
            return await self._call(self.page_tool, url=url, max_chars=max_chars, focus=focus)
        except MCPBackendError as exc:
            # Preserve known page validation reasons at the business boundary;
            # transport errors and unknown backend details remain distinct.
            if str(exc) in PAGE_READ_ERROR_CODES:
                raise PageReadError(str(exc)) from None
            raise
        except Exception:
            # This is the MCP call/payload layer, not an HTTP page response.
            # Keep the original safe RPC category in calls and expose a stable
            # transport reason to the Agent without forwarding exception text.
            raise PageReadError("page_transport_error") from None
