"""Bounded HTTP observations for isolated diagnostics, never production imports."""

import math
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from agentscope.mcp import MCPClient, StdioMCPConfig

from as_researcher.mcp_bridge import MCPBackends

MAX_EVENTS = 16
ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTIC_SERVER_ARGS = ["-m", "scripts.search_diagnostic_server"]


class HttpObservation:
    """A thread-safe snapshot of finished requests; in-flight work stays explicit."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started = 0
        self._finished = 0
        self._events = []

    def __enter__(self):
        from ddgs.exceptions import TimeoutException
        from ddgs.http_client import HttpClient

        original = HttpClient.request

        def observed(client, *args, **kwargs):
            with self._lock:
                self._started += 1
            started = time.perf_counter()
            event = {}
            try:
                response = original(client, *args, **kwargs)
                status = response.status_code
                event["http_status"] = (
                    status if type(status) is int and 100 <= status <= 599 else None
                )
                event["response_bytes"] = len(response.content)
                return response
            except Exception as exc:
                event["error_code"] = (
                    "http_timeout" if isinstance(exc, TimeoutException) else "http_client_error"
                )
                raise
            finally:
                event["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
                with self._lock:
                    self._finished += 1
                    if len(self._events) < MAX_EVENTS:
                        self._events.append(event)

        self._patch = patch.object(HttpClient, "request", observed)
        self._patch.start()
        return self

    def __exit__(self, *args):
        self._patch.stop()

    def snapshot(self):
        with self._lock:
            return {
                "schema_version": 1,
                "started": self._started,
                "finished": self._finished,
                "in_flight": self._started - self._finished,
                "dropped": self._finished - len(self._events),
                "events": [dict(event) for event in self._events],
            }


def validate_observation(payload):
    """Reject unexpected fields/types before persisting subprocess diagnostics."""
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "started",
        "finished",
        "in_flight",
        "dropped",
        "events",
    }:
        raise ValueError("Invalid diagnostic snapshot")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("Invalid diagnostic version")
    if any(
        type(payload[k]) is not int or payload[k] < 0
        for k in ("started", "finished", "in_flight", "dropped")
    ):
        raise ValueError("Invalid diagnostic counts")
    events = payload["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError("Invalid diagnostic events")
    if (
        payload["started"] != payload["finished"] + payload["in_flight"]
        or payload["finished"] != len(events) + payload["dropped"]
    ):
        raise ValueError("Inconsistent diagnostic counts")
    for event in events:
        if not isinstance(event, dict) or set(event) not in (
            {"http_status", "response_bytes", "duration_ms"},
            {"error_code", "duration_ms"},
        ):
            raise ValueError("Invalid HTTP event")
        duration = event["duration_ms"]
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            raise ValueError("Invalid HTTP duration")
        if "error_code" in event:
            if event["error_code"] not in ("http_timeout", "http_client_error"):
                raise ValueError("Invalid HTTP error")
        else:
            status = event["http_status"]
            if status is not None and (type(status) is not int or not 100 <= status <= 599):
                raise ValueError("Invalid HTTP status")
            if type(event["response_bytes"]) is not int or event["response_bytes"] < 0:
                raise ValueError("Invalid response size")
    return payload


def diagnostic_remote():
    # Only this diagnostic selects a wrapper. Production MCPBackends continues
    # to launch as_researcher.mcp_server with its unchanged tool schemas.
    remote = MCPBackends("duckduckgo", "none")
    remote.client = MCPClient(
        name="research",
        is_stateful=True,
        execution_timeout=35,
        mcp_config=StdioMCPConfig(
            command=sys.executable,
            args=DIAGNOSTIC_SERVER_ARGS,
            cwd=str(ROOT),
            env={
                "PYTHONIOENCODING": "utf-8",
                "RESEARCH_SEARCH_PROVIDER": "duckduckgo",
                "RESEARCH_READER": "none",
            },
        ),
    )
    return remote


async def read_observation(remote):
    tool = await remote.client.get_tool("http_observation")
    return validate_observation(await remote._call(tool))
