"""Native framework input and output boundary checks, not model quality tests."""

import json
import unittest

import httpx
from agentscope.credential import OpenAICredential

from as_researcher.core import source_id
from as_researcher.models import ObservedOpenAIChatModel
from as_researcher.researcher import SYSTEM_PROMPT, render_report
from scripts.observe_answer_budget import AnswerStageModel, observe


def synthetic_run():
    url = "https://example.org/shield"
    sid = source_id(url)
    return {
        "topic": "解释调用方取消与内部任务的区别。",
        "answer": "这是原始固定超长回答。" * 100 + f"[{sid}@page]",
        "page_evidence": [
            {
                "id": sid,
                "url": url,
                "content": "Synthetic evidence: caller cancellation does not cancel inner task; await still raises.",
                "code_selection": {
                    "scope": "ranked_candidates_only",
                    "retained": [],
                    "omitted": [],
                },
            }
        ],
    }


class AnswerBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_prompt_reaches_initial_and_answer_turn_without_cutting_output(self):
        source = synthetic_run()
        run, observation = await observe(source)
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(run["answer"], source["answer"])
        self.assertFalse(run["metrics"]["answer_within_limit"])
        self.assertEqual(run["revision"]["status"], "disabled")
        self.assertEqual(run["metrics"]["fixture_model_calls"], 2)
        self.assertTrue(any("超过非空白字符预算" in w for w in run["metrics"]["warnings"]))

    async def test_unbounded_prompt_and_supplied_source_policy_are_preserved(self):
        run, observation = await observe(synthetic_run(), maximum=None)
        self.assertTrue(observation["passed"])
        prompt = observation["model_turns"][0]["system_prompt"]
        self.assertTrue(prompt.startswith(SYSTEM_PROMPT))
        self.assertIn("报告结构：结论、证据与比较", prompt)
        self.assertNotIn("字符预算下的输出约定", prompt)
        self.assertIn("URL列表不是搜索摘要或正文", prompt)
        self.assertEqual(run["config"]["max_answer_chars"], None)

    async def test_configured_budget_and_condition_policy_reach_actual_sdk_request(self):
        run, observation, requests = await self.delegated()
        self.assertTrue(observation["passed"])
        self.assertEqual(run["mode"], "live_model_fixture")
        self.assertIn("真实模型", render_report(run))
        self.assertEqual(len(requests), 1)
        content = requests[0]["messages"][0]["content"]
        text = content if isinstance(content, str) else "".join(b["text"] for b in content)
        self.assertIn("不超过 250", text)
        self.assertIn("保留问题所需的条件、否定、例外", text)
        self.assertIn("不能把所有引用堆到文末", text)
        self.assertNotIn("报告结构：结论、证据与比较", text)
        self.assertEqual(requests[0]["tool_choice"], "none")
        self.assertFalse(requests[0].get("tools"))
        self.assertEqual(requests[0]["max_tokens"], 1800)
        self.assertEqual(run["metrics"]["fixture_model_calls"], 1)
        self.assertEqual(len(run["provider_responses"]), 1)
        self.assertTrue(run["metrics"]["answer_within_limit"])

    async def test_unexpected_tool_response_is_not_executed_or_retried(self):
        run, observation, requests = await self.delegated(tool_request=True)
        self.assertEqual(len(requests), 1)
        self.assertEqual(run["metrics"]["page_read_attempts"], 1)
        self.assertEqual(observation["delegated_answer_requests"], 1)
        self.assertEqual(run["metrics"]["status"], "model_error")

    async def delegated(self, tool_request=False):
        source = synthetic_run()
        sid = source["page_evidence"][0]["id"]
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            message = {
                "role": "assistant",
                "content": f"调用方取消不取消内部任务，await仍抛出异常 [{sid}@page]。",
            }
            if tool_request:
                message["tool_calls"] = [
                    {
                        "id": "forbidden",
                        "type": "function",
                        "function": {
                            "name": "read_page",
                            "arguments": json.dumps({"source_id": sid}),
                        },
                    }
                ]
            return httpx.Response(
                200,
                json={
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if tool_request else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = ObservedOpenAIChatModel(
                credential=OpenAICredential(
                    api_key="fixture-key", base_url="https://model.invalid/v1"
                ),
                model="fixture",
                stream=False,
                max_retries=0,
                extra_body={"max_tokens": 1800},
                client_kwargs={"http_client": client, "max_retries": 0},
            )
            run, observation = await observe(source, maximum=250, delegate=model)
        return run, observation, requests

    async def test_harness_refuses_a_second_answer_call_before_invoking_delegate(self):
        model = AnswerStageModel("SRC-12345678", "fixture")
        model.calls = 2
        with self.assertRaisesRegex(RuntimeError, "No second answer request"):
            await model._call_api("fixture", [])
        self.assertEqual(model.answer_calls, 0)
