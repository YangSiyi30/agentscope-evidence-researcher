"""Diagnostic execution boundaries, safe observations and explicit one-path selection."""

import asyncio
import contextlib
import hashlib
import importlib.metadata
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from as_researcher.core import Source
from as_researcher.search import SearchBackendError
from scripts import probe_search as probe


def plan():
    return {
        "schema_version": 2,
        "kind": "search_transport_diagnostic",
        "provider": "duckduckgo",
        "transports": ["local", "mcp"],
        "query": "same public test query",
        "result_limit": 3,
        "search_timeout": 20,
        "source_sha256": probe.source_hash(),
        "probe_sha256": hashlib.sha256(Path(probe.__file__).read_bytes()).hexdigest(),
        "ddgs_version": importlib.metadata.version("ddgs"),
        "diagnostic_files_sha256": {
            name: hashlib.sha256((probe.ROOT / name).read_bytes()).hexdigest()
            for name in probe.DIAGNOSTIC_FILES
        },
    }


class SearchProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_default_plan_does_not_construct_backends_or_write_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(plan()), "utf-8")
            with (
                patch.object(probe, "DDGSearch") as local,
                patch.object(probe, "diagnostic_remote") as mcp,
                patch("pathlib.Path.mkdir") as mkdir,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(probe.main(["--plan", str(path)]), 0)
                local.assert_not_called()
                mcp.assert_not_called()
                mkdir.assert_not_called()

    def test_bad_plans_and_missing_transport_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            for updates in (
                {"source_sha256": "wrong"},
                {"probe_sha256": "wrong"},
                {"diagnostic_files_sha256": {}},
                {"ddgs_version": "wrong"},
                {"provider": "other"},
                {"query": " "},
                {"result_limit": True},
                {"search_timeout": 21},
            ):
                path.write_text(json.dumps(plan() | updates), "utf-8")
                with (
                    patch.object(probe, "DDGSearch") as local,
                    patch.object(probe, "diagnostic_remote") as mcp,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit):
                        probe.main(["--plan", str(path), "--execute", "--transport", "local"])
                    local.assert_not_called()
                    mcp.assert_not_called()
            path.write_text(json.dumps(plan()), "utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                probe.main(["--plan", str(path), "--execute"])

    async def test_local_observation_omits_body_headers_urls_and_secrets(self):
        from ddgs.http_client import HttpClient

        secret = "must-not-persist-from-headers-body-or-title"
        calls = []

        async def backend(query, limit):
            calls.append((query, limit))
            await asyncio.to_thread(
                HttpClient.request,
                object(),
                "POST",
                "https://example.org/",
                headers={"Authorization": secret},
            )
            return [Source(secret, "https://example.org/?secret=" + secret, secret)]

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    HttpClient,
                    "request",
                    return_value=SimpleNamespace(status_code=200, content=secret.encode()),
                ),
                patch.object(probe, "DDGSearch", return_value=backend),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                p = plan()
                self.assertEqual(
                    await probe.execute(json.dumps(p).encode(), p, "local", Path(tmp)), 0
                )
            data = json.loads(next(Path(tmp).rglob("result.json")).read_text("utf-8"))
            self.assertEqual(calls, [(p["query"], 3)])
            self.assertEqual(data["logical_search_attempts"], 1)
            self.assertEqual(data["http_observation"]["events"][0]["http_status"], 200)
            self.assertEqual(data["model_calls"], 0)
            self.assertNotIn(secret, json.dumps(data))

    async def test_snapshot_failure_does_not_replace_search_failure_or_invent_http(self):
        class Remote:
            def __init__(self):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def search(self, query, limit):
                self.calls.append(
                    {
                        "tool": "mcp__research__search",
                        "status": "error",
                        "search_error_code": "search_no_results_or_unavailable",
                        "raw_error": "secret-not-saved",
                    }
                )
                raise SearchBackendError("search_no_results_or_unavailable")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(probe, "diagnostic_remote", Remote),
            patch.object(
                probe, "read_observation", AsyncMock(side_effect=ValueError("secret-not-saved"))
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            p = plan()
            self.assertEqual(await probe.execute(json.dumps(p).encode(), p, "mcp", Path(tmp)), 2)
            data = json.loads(next(Path(tmp).rglob("result.json")).read_text("utf-8"))
            self.assertEqual(len(data["mcp_calls"]), 1)
            self.assertEqual(data["error_code"], "search_no_results_or_unavailable")
            self.assertIsNone(data["http_observation"])
            self.assertEqual(data["http_observation_error"], "snapshot_unavailable_or_invalid")
            self.assertNotIn("secret-not-saved", json.dumps(data))

    async def test_explicit_cancellation_is_recorded_and_propagated(self):
        started = asyncio.Event()

        async def slow(*args):
            started.set()
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as tmp, patch.object(probe, "local_search", slow):
            p = plan()
            task = asyncio.create_task(probe.execute(json.dumps(p).encode(), p, "local", Path(tmp)))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            data = json.loads(next(Path(tmp).rglob("result.json")).read_text("utf-8"))
            self.assertEqual(data["status"], "cancelled")
            self.assertEqual(data["logical_search_attempts"], 1)
