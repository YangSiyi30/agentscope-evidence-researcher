"""One request via the pinned adapter; no live service or semantic quality assertions."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from agentscope.credential import OpenAICredential
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse

from as_researcher.models import ObservedOpenAIChatModel
from scripts import execute_minimal_edit as edit


class FakeModel:
    stream = False
    max_retries = 0
    model = "deepseek-flash"
    thinking_mode = "disabled"

    def __init__(self, text, mode="stop"):
        self.text, self.mode, self.calls = text, mode, []
        self.provider_responses = []
        self.client = SimpleNamespace(close=AsyncMock())

    async def __call__(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.mode == "error":
            raise httpx.ConnectError("secret-credential-and-url")
        if self.mode == "cancel":
            raise asyncio.CancelledError
        if self.mode == "wait_for_external_cancel":
            await asyncio.sleep(10)
        if self.mode in ("timeout", "timeout_return"):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                if self.mode == "timeout":
                    raise
        self.provider_responses.append(
            {
                "finish_reason": self.mode
                if self.mode in ("length", "content_filter")
                else ("unknown-private-value" if self.mode == "unknown_finish" else "stop"),
                "input_tokens": None if self.mode == "unknown_usage" else 20,
                "output_tokens": None if self.mode == "unknown_usage" else 10,
                "has_tool_calls": self.mode == "tool_request",
                "incomplete": self.mode in ("length", "content_filter"),
                "raw": "secret-credential-and-url",
            }
        )
        if self.mode == "multiple_events":
            self.provider_responses.append(dict(self.provider_responses[-1]))
        if self.mode == "tool_request":
            return ChatResponse(
                content=[
                    TextBlock(text=self.text),
                    ToolCallBlock(
                        id="bad", name="never_execute", input="secret-credential-and-url"
                    ),
                ],
                is_last=True,
            )
        return ChatResponse(content=[TextBlock(text=self.text)], is_last=True)


class MinimalEditExecutionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = edit.check.load_context(edit.check.DEFAULT_FIXTURE)
        cls.good = cls.context["fixture"]["handwritten_candidates"][0]["text"]

    def test_plan_only_constructs_nothing_and_does_not_load_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(edit.make_plan(self.context)), "utf-8")
            with (
                patch.object(edit, "live_model") as model,
                patch.object(edit, "load_dotenv") as env,
                patch.object(Path, "mkdir") as mkdir,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(edit.main(["--plan", str(path)]), 0)
                model.assert_not_called()
                env.assert_not_called()
                mkdir.assert_not_called()

    def test_stale_input_source_dependencies_and_executor_rejected_preflight(self):
        for key, value in (
            ("input_sha256", "wrong"),
            ("system_sha256", "wrong"),
            ("source_sha256", "wrong"),
            ("executor_files_sha256", {}),
            ("dependencies", {}),
            ("bound_files_sha256", {}),
            ("fixture_sha256", "wrong"),
            ("application_version", "old"),
            ("expected_model", "another-model"),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "plan.json"
                path.write_text(json.dumps(edit.make_plan(self.context) | {key: value}), "utf-8")
                with (
                    patch.object(edit, "live_model") as model,
                    patch.object(edit, "load_dotenv") as env,
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    edit.main(["--plan", str(path), "--execute"])
                model.assert_not_called()
                env.assert_not_called()

    def test_noninteger_or_expanded_budgets_rejected(self):
        for key, value in (
            ("timeout_seconds", 46),
            ("output_tokens", 1201),
            ("model_calls", 2),
            ("retries", 1),
            ("output_tokens", True),
            ("answer_characters", 800),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "plan.json"
                plan = edit.make_plan(self.context)
                plan["budget"][key] = value
                path.write_text(json.dumps(plan), "utf-8")
                with self.assertRaises(ValueError):
                    edit.load_plan(path)

    async def test_real_adapter_mock_http_sends_one_no_tool_bounded_request(self):
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "deepseek-flash",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": " \n" + self.good},
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = ObservedOpenAIChatModel(
                credential=OpenAICredential(
                    api_key="secret-credential-and-url", base_url="https://model.invalid/v1"
                ),
                model="deepseek-flash",
                stream=False,
                max_retries=0,
                client_kwargs={"http_client": client, "max_retries": 0},
            )
            model.thinking_mode = "disabled"
            record = await edit.run_once(edit.make_plan(self.context), self.context, model)
        self.assertEqual(len(requests), 1)
        self.assertFalse(requests[0].get("tools"))
        self.assertEqual(requests[0]["tool_choice"], "none")
        self.assertEqual(requests[0]["max_tokens"], 1200)
        self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
        self.assertTrue(record["ready_for_semantic_review"])
        self.assertEqual(record["response_text"], " \n" + self.good)
        self.assertEqual(record["token_usage"], {"input": 20, "output": 10})
        self.assertNotIn("secret-credential-and-url", json.dumps(record))

    async def test_unknown_usage_is_null_without_preventing_structural_review(self):
        record = await edit.run_once(
            edit.make_plan(self.context), self.context, FakeModel(self.good, "unknown_usage")
        )
        self.assertTrue(record["ready_for_semantic_review"])
        self.assertIsNone(record["token_usage"])
        self.assertIsNone(record["semantic_review"]["assessment"])

    async def test_incomplete_unknown_or_multiple_events_never_mark_completion(self):
        for mode in ("length", "content_filter", "unknown_finish", "multiple_events"):
            record = await edit.run_once(
                edit.make_plan(self.context), self.context, FakeModel(self.good, mode)
            )
            self.assertIn("edit_completion_unverified", record["errors"])
            self.assertFalse(record["ready_for_semantic_review"])
            self.assertEqual(record["response_text"], self.good)
        for event in (
            {"finish_reason": "stop"},
            {
                "finish_reason": "secret-credential-and-url",
                "input_tokens": -1,
                "output_tokens": True,
            },
        ):
            rows = edit.safe_events([event])
            self.assertIsNone(rows[0]["has_tool_calls"])
            self.assertNotIn("secret-credential-and-url", json.dumps(rows))

    async def test_timeout_even_when_model_swallows_cancellation_is_rejected(self):
        for mode in ("timeout", "timeout_return"):
            plan = edit.make_plan(self.context)
            plan["budget"]["timeout_seconds"] = 0.01
            model = FakeModel(self.good, mode)
            record = await edit.run_once(plan, self.context, model)
            self.assertIn("edit_timeout", record["errors"])
            self.assertEqual(len(model.calls), 1)
            self.assertFalse(record["ready_for_semantic_review"])

    async def test_exception_retains_type_and_unknown_usage_no_secret_no_retry(self):
        model = FakeModel(self.good, "error")
        record = await edit.run_once(edit.make_plan(self.context), self.context, model)
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(len(model.calls), 1)
        self.assertIn("edit_model_error", record["errors"])
        self.assertIsNone(record["token_usage"])
        self.assertIsNone(record["response_text"])
        self.assertNotIn("secret-credential-and-url", json.dumps(record))

    async def test_cancel_persists_record_and_closes_before_propagating(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FakeModel(self.good, "cancel")
            plan = edit.make_plan(self.context)
            with (
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(asyncio.CancelledError),
            ):
                await edit.execute(
                    json.dumps(plan).encode(), plan, self.context, Path(tmp), lambda: model
                )
            record = json.loads(next(Path(tmp).rglob("edit.json")).read_bytes())
            self.assertEqual(record["status"], "cancelled")
            self.assertIn("caller_cancelled", record["errors"])
            model.client.close.assert_awaited_once()

    async def test_wrong_layer_whole_answer_empty_and_semantic_error_boundaries(self):
        for text, valid in (
            (self.good.replace("@page", "@snippet"), False),
            (self.context["answer"], False),
            (" \n", False),
            (self.context["fixture"]["handwritten_candidates"][1]["text"], True),
        ):
            record = await edit.run_once(
                edit.make_plan(self.context), self.context, FakeModel(text)
            )
            self.assertEqual(record["ready_for_semantic_review"], valid)
            self.assertIsNone(record["semantic_review"]["assessment"])
            self.assertFalse(record["applied_to_production"])

    async def test_wrong_model_policy_prevents_attempt(self):
        for updates in (
            {"stream": True},
            {"max_retries": 1},
            {"model": "wrong"},
            {"thinking_mode": "provider-default"},
        ):
            model = FakeModel(self.good)
            for key, value in updates.items():
                setattr(model, key, value)
            record = await edit.run_once(edit.make_plan(self.context), self.context, model)
            self.assertEqual(record["attempts"], 0)
            self.assertEqual(len(model.calls), 0)

    async def test_tool_request_is_recorded_without_executing_or_archiving_arguments(self):
        record = await edit.run_once(
            edit.make_plan(self.context), self.context, FakeModel(self.good, "tool_request")
        )
        self.assertIn("edit_tool_request", record["errors"])
        self.assertFalse(record["ready_for_semantic_review"])
        self.assertEqual(record["tools_executed"], 0)
        self.assertNotIn("secret-credential-and-url", json.dumps(record))

    async def test_actual_external_task_cancel_saves_and_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FakeModel(self.good, "wait_for_external_cancel")
            plan = edit.make_plan(self.context)
            with contextlib.redirect_stdout(io.StringIO()):
                task = asyncio.create_task(
                    edit.execute(
                        json.dumps(plan).encode(), plan, self.context, Path(tmp), lambda: model
                    )
                )
                while not model.calls:
                    await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            saved = json.loads(next(Path(tmp).rglob("edit.json")).read_bytes())
            self.assertEqual(saved["status"], "cancelled")
            self.assertIsNone(saved["token_usage"])
            model.client.close.assert_awaited_once()

    async def test_execution_uses_independent_folders_and_safe_live_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = edit.make_plan(self.context)
            raw = json.dumps(plan).encode()
            models = []
            for _ in range(2):
                model = FakeModel(self.good)
                models.append(model)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        await edit.execute(raw, plan, self.context, Path(tmp), lambda: model), 0
                    )
            self.assertEqual(len(list(Path(tmp).rglob("edit.json"))), 2)
            self.assertTrue(all(len(model.calls) == 1 for model in models))
            record = edit.new_record(plan, self.context)
            record["response_text"] = "<script>bad</script>"
            page = edit.render_record(record)
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("<script>", page)

    async def test_cleanup_failure_recorded_separately_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FakeModel(self.good)
            model.client.close.side_effect = RuntimeError("secret-credential-and-url")
            plan = edit.make_plan(self.context)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    await edit.execute(
                        json.dumps(plan).encode(), plan, self.context, Path(tmp), lambda: model
                    ),
                    2,
                )
            record = json.loads(next(Path(tmp).rglob("edit.json")).read_bytes())
            self.assertEqual(record["cleanup_error_type"], "RuntimeError")
            self.assertEqual(record["response_text"], self.good)
            self.assertNotIn("secret-credential-and-url", json.dumps(record))

    async def test_configured_model_mismatch_prevents_client_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = edit.make_plan(self.context)
            with (
                patch.dict("os.environ", {"MODEL_NAME": "wrong"}),
                patch.object(edit, "load_dotenv"),
                patch.object(edit, "live_model") as factory,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    await edit.execute(json.dumps(plan).encode(), plan, self.context, Path(tmp)), 2
                )
            factory.assert_not_called()
            record = json.loads(next(Path(tmp).rglob("edit.json")).read_bytes())
            self.assertEqual(record["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
