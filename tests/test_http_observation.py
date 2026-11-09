"""Safe subprocess observations, bounded snapshots and production separation."""

import asyncio
import contextlib
import copy
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ddgs.exceptions import TimeoutException
from ddgs.http_client import HttpClient

from as_researcher.mcp_bridge import MCPBackends
from scripts import probe_search as probe
from scripts import search_diagnostics as diag


class HttpObservationTests(unittest.IsolatedAsyncioTestCase):
    def test_errors_are_sanitized_and_original_hook_restored(self):
        with patch.object(
            HttpClient, "request", side_effect=TimeoutException("secret-body-or-url")
        ):
            original = HttpClient.request
            observer = diag.HttpObservation()
            with self.assertRaises(TimeoutException), observer:
                HttpClient.request(object())
            self.assertIs(HttpClient.request, original)
            snapshot = diag.validate_observation(observer.snapshot())
            self.assertEqual(snapshot["events"][0]["error_code"], "http_timeout")
            self.assertEqual(
                (snapshot["started"], snapshot["finished"], snapshot["in_flight"]), (1, 1, 0)
            )
            self.assertNotIn("secret-body-or-url", json.dumps(snapshot))

    def test_snapshot_is_bounded_and_dropped_count_explicit(self):
        with patch.object(
            HttpClient, "request", return_value=SimpleNamespace(status_code=202, content=b"secret")
        ):
            with diag.HttpObservation() as observer:
                for _ in range(diag.MAX_EVENTS + 2):
                    HttpClient.request(object())
            snapshot = diag.validate_observation(observer.snapshot())
            self.assertEqual(len(snapshot["events"]), diag.MAX_EVENTS)
            self.assertEqual(snapshot["dropped"], 2)
            self.assertEqual(snapshot["events"][0]["http_status"], 202)
            snapshot["events"].clear()
            self.assertEqual(len(observer.snapshot()["events"]), diag.MAX_EVENTS)

    async def test_in_flight_requests_are_not_reported_as_finished(self):
        started, release = threading.Event(), threading.Event()

        def slow(*args):
            started.set()
            release.wait(timeout=2)
            return SimpleNamespace(status_code=200, content=b"fixture")

        with patch.object(HttpClient, "request", slow), diag.HttpObservation() as observer:
            task = asyncio.create_task(asyncio.to_thread(HttpClient.request, object()))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                snapshot = diag.validate_observation(observer.snapshot())
                self.assertEqual(
                    (snapshot["started"], snapshot["finished"], snapshot["in_flight"]), (1, 0, 1)
                )
                self.assertEqual(snapshot["events"], [])
            finally:
                release.set()
                await task
            self.assertEqual(observer.snapshot()["in_flight"], 0)

    def test_subprocess_snapshot_rejects_unknown_fields_bad_types_and_counts(self):
        baseline = {
            "schema_version": 1,
            "started": 1,
            "finished": 1,
            "in_flight": 0,
            "dropped": 0,
            "events": [{"http_status": 200, "response_bytes": 2, "duration_ms": 1.0}],
        }
        for updates in (
            {"headers": "secret"},
            {"schema_version": True},
            {"started": True},
            {"finished": 2},
            {"events": [{"http_status": 200, "response_bytes": 2, "duration_ms": float("nan")}]},
            {"events": [{"http_status": True, "response_bytes": 2, "duration_ms": 1}]},
            {"events": [{"error_code": "secret", "duration_ms": 1}]},
            {
                "events": [
                    {"http_status": 200, "response_bytes": 2, "duration_ms": 1, "body": "secret"}
                ]
            },
        ):
            with self.assertRaises(ValueError):
                diag.validate_observation(copy.deepcopy(baseline) | updates)

    async def test_real_stdio_wrapper_observes_success_and_ambiguous_failure(self):
        fixture = diag.ROOT / "tests/fixtures/diagnostic_search_server.py"
        for query, expected_code in (("success", 0), ("timeout", 2)):
            with (
                tempfile.TemporaryDirectory() as tmp,
                patch.object(diag, "DIAGNOSTIC_SERVER_ARGS", [str(fixture)]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                plan = {
                    "transports": ["local", "mcp"],
                    "provider": "duckduckgo",
                    "query": query,
                    "result_limit": 3,
                    "search_timeout": 2,
                    "source_sha256": "test",
                    "ddgs_version": "fixture",
                }
                self.assertEqual(
                    await probe.execute(json.dumps(plan).encode(), plan, "mcp", Path(tmp)),
                    expected_code,
                )
                data = json.loads(next(Path(tmp).rglob("result.json")).read_bytes())
                snapshot = diag.validate_observation(data["http_observation"])
                self.assertEqual(
                    (snapshot["started"], snapshot["finished"], snapshot["in_flight"]), (1, 1, 0)
                )
                self.assertEqual(data["logical_search_attempts"], 1)
                self.assertEqual(
                    [e["tool"] for e in data["mcp_calls"]],
                    ["mcp__research__search", "mcp__research__http_observation"],
                )
                if query == "timeout":
                    self.assertEqual(snapshot["events"][0]["error_code"], "http_timeout")
                    self.assertEqual(data["error_code"], "search_no_results_or_unavailable")
                else:
                    self.assertEqual(snapshot["events"][0]["http_status"], 200)
                self.assertNotIn("fixture-secret-never-persist", json.dumps(data))

    async def test_production_server_has_no_diagnostic_rpc(self):
        async with MCPBackends("fixture", "none") as remote:
            self.assertEqual(set(remote.discovered_tools), {"search", "read_public_page"})
