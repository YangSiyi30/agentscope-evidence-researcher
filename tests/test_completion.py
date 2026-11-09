"""Mock the HTTP boundary, keeping the actual SDK, model parser and Agent loop."""

import json
import logging
import unittest

import httpx
from agentscope.credential import OpenAICredential

from as_researcher.models import ObservedOpenAIChatModel, live_model
from as_researcher.researcher import run_research
from as_researcher.search import FixtureSearch

logging.getLogger("as").setLevel(logging.CRITICAL)


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(
        self, reason, *, tools=False, text="只返回一部分结论", reuse=False, choices=True
    ):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            message = {
                "role": "assistant",
                "content": text,
                "reasoning_content": "private-reasoning-marker",
            }
            if tools:
                message["tool_calls"] = [
                    {
                        "id": "cut",
                        "type": "function",
                        "function": {"name": "search_web", "arguments": '{"query":'},
                    }
                ]
            return httpx.Response(
                200,
                json={
                    "id": "test-response",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test",
                    "choices": [{"index": 0, "message": message, "finish_reason": reason}]
                    if choices
                    else [],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 12,
                        "total_tokens": 32,
                        "completion_tokens_details": {"reasoning_tokens": 7},
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = ObservedOpenAIChatModel(
                credential=OpenAICredential(
                    api_key="secret-marker", base_url="https://model.invalid/v1"
                ),
                model="test",
                stream=False,
                max_retries=0,
                client_kwargs={"http_client": client, "max_retries": 0},
            )
            backend = FixtureSearch()
            result = await run_research("test", model, backend, mode="mock_http")
            if reuse:
                result = await run_research("second", model, backend, mode="mock_http")
        self.assertEqual(len(requests), 2 if reuse else 1)
        self.assertEqual(backend.calls, [])
        self.assertNotIn("private-reasoning-marker", json.dumps(result))
        self.assertNotIn("secret-marker", json.dumps(result))
        return result

    async def test_length_preserves_partial_text_but_marks_incomplete(self):
        result = await self.exercise("length")
        self.assertEqual(result["answer"], "只返回一部分结论")
        self.assertEqual(result["metrics"]["agent_finished_reason"], "completed")
        self.assertEqual(result["metrics"]["status"], "model_output_truncated")
        self.assertEqual(result["metrics"]["completion_check"], "incomplete")
        self.assertEqual(result["provider_responses"][0]["reasoning_tokens"], 7)
        self.assertEqual(result["metrics"]["token_usage"], {"input": 20, "output": 12})

    async def test_truncated_tool_arguments_never_execute_or_retry(self):
        result = await self.exercise("length", tools=True, text=None)
        self.assertEqual(result["metrics"]["status"], "model_output_truncated")
        self.assertTrue(result["provider_responses"][0]["tool_calls_suppressed"])
        self.assertEqual(result["metrics"]["tool_invocations"], 0)

    async def test_content_filter_unknown_and_inconsistent_finish_are_incomplete(self):
        for reason, has_tools in [
            ("content_filter", False),
            ("secret-marker", False),
            ("stop", True),
            ("tool_calls", False),
        ]:
            with self.subTest(reason=reason):
                result = await self.exercise(reason, tools=has_tools)
                self.assertEqual(result["metrics"]["status"], "model_response_incomplete")

    async def test_no_choices_is_not_a_completed_report(self):
        result = await self.exercise("stop", choices=False)
        self.assertEqual(result["metrics"]["status"], "model_response_incomplete")

    async def test_stop_is_only_transport_completion_not_quality(self):
        result = await self.exercise("stop")
        self.assertEqual(result["metrics"]["status"], "completed")
        self.assertEqual(
            result["metrics"]["completion_check"], "provider_stopped_quality_unverified"
        )
        self.assertTrue(result["metrics"]["warnings"])

    async def test_reused_model_records_only_current_run_responses(self):
        result = await self.exercise("length", reuse=True)
        self.assertEqual(len(result["provider_responses"]), 1)
        self.assertEqual(result["provider_responses"][0]["response_index"], 2)

    def test_token_budget_validation_before_client_creation(self):
        for limit in [0, 63, 4097, True]:
            with self.assertRaises(ValueError):
                live_model(max_tokens=limit)
