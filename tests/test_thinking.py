"""Exercise the real SDK HTTP body, without a provider call or secret loading."""

import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from as_researcher.core import ResearchConfig
from as_researcher.models import live_model, thinking_parameters
from as_researcher.revision import revise_answer, revision_integrity_errors
from tests.test_comparison import example


class ThinkingTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, mode="disabled", finish="stop", configured_mode=None):
        run, fixture_revision = example()
        run["sources"][0]["title"] = "Synthetic documentation v2; title is metadata, not body"
        run["answer"] += " [SRC-12345678@snippet]"
        fixture_revision["candidate_answer"] += " [SRC-12345678@snippet]"
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "thinking-fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "deepseek-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": fixture_revision["candidate_answer"],
                            },
                            "finish_reason": finish,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 60,
                        "total_tokens": 160,
                        "completion_tokens_details": {"reasoning_tokens": 0},
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with (
                patch.dict(
                    os.environ,
                    {
                        "LLM_API_KEY": "secret-fixture-never-persist",
                        "LLM_BASE_URL": "https://api.deepseek.com",
                        "LLM_MODEL": "deepseek-flash",
                    },
                    clear=True,
                ),
                patch(
                    "as_researcher.models.httpx", SimpleNamespace(AsyncClient=lambda **_: client)
                ),
            ):
                model = live_model(max_tokens=2400, thinking_mode=configured_mode or mode)
            # Deliberately differ from the constructor to exercise the per-call override.
            config = ResearchConfig(
                revise_answer=True,
                max_answer_chars=100,
                revision_max_tokens=1200,
                revision_thinking=mode,
            )
            record = await revise_answer(
                "mock fixture", run["answer"], run["sources"], run["page_evidence"], config, model
            )
        self.assertNotIn("secret-fixture-never-persist", json.dumps(record))
        return record, requests

    async def test_disabled_reaches_wire_with_token_cap_and_no_tools(self):
        record, requests = await self.exercise()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
        self.assertEqual(requests[0]["max_tokens"], 1200)
        self.assertFalse(requests[0].get("tools"))
        self.assertEqual(requests[0]["tool_choice"], "none")
        self.assertNotIn("reasoning_effort", requests[0])
        self.assertTrue(record["applied"])
        self.assertEqual(record["provider_responses"][0]["reasoning_tokens"], 0)
        saved = json.loads(record["request"]["user"])["saved_evidence"]
        snippet = next(e for e in saved if e["citation"].endswith("@snippet]"))
        self.assertEqual(
            snippet["title"], "Synthetic documentation v2; title is metadata, not body"
        )
        self.assertEqual(snippet["content"], "Only a discovery snippet.")
        self.assertEqual(
            record["generation_parameters"],
            {
                "max_tokens": 1200,
                "thinking": {"type": "disabled"},
            },
        )
        self.assertEqual(revision_integrity_errors(record), [])
        changed = copy.deepcopy(record)
        changed["generation_parameters"].pop("thinking")
        self.assertIn("revision_generation_parameters_mismatch", revision_integrity_errors(changed))

    async def test_default_omits_thinking_and_preserves_legacy_wire_policy(self):
        record, requests = await self.exercise(mode="provider-default")
        self.assertTrue(record["applied"])
        self.assertNotIn("thinking", requests[0])
        self.assertNotIn("reasoning_effort", requests[0])
        self.assertEqual(record["generation_parameters"], {"max_tokens": 1200})

    async def test_length_is_rejected_even_when_thinking_disabled(self):
        record, requests = await self.exercise(finish="length")
        self.assertEqual(len(requests), 1)
        self.assertFalse(record["applied"])
        self.assertIn("revision_completion_unverified", record["errors"])
        self.assertTrue(record["original_answer"])

    async def test_mismatched_model_policy_skips_before_http(self):
        record, requests = await self.exercise(configured_mode="provider-default")
        self.assertEqual(requests, [])
        self.assertEqual(record["attempts"], 0)
        self.assertEqual(record["errors"], ["revision_thinking_configuration_mismatch"])

    def test_unverified_endpoint_model_or_mode_rejected_before_client_creation(self):
        for endpoint, model in (
            ("https://proxy.invalid/v1", "deepseek-flash"),
            ("https://api.deepseek.com.proxy.invalid", "deepseek-flash"),
            ("http://api.deepseek.com", "deepseek-flash"),
            ("https://api.deepseek.com/other", "deepseek-flash"),
            ("https://api.deepseek.com?key=secret", "deepseek-flash"),
            ("https://api.deepseek.com", "unverified-alias"),
        ):
            with (
                self.subTest(endpoint=endpoint, model=model),
                patch.dict(
                    os.environ,
                    {
                        "MODEL_API_KEY": "dummy",
                        "MODEL_BASE_URL": endpoint,
                        "MODEL_NAME": model,
                    },
                    clear=True,
                ),
                patch("as_researcher.models.httpx.AsyncClient") as client,
            ):
                with self.assertRaises(ValueError):
                    live_model(thinking_mode="disabled")
                client.assert_not_called()
        with self.assertRaises(ValueError):
            thinking_parameters("low", "https://api.deepseek.com", "deepseek-flash")
        self.assertEqual(
            thinking_parameters("provider-default", "https://proxy.invalid", "any"), {}
        )
        self.assertEqual(
            thinking_parameters("disabled", "https://api.deepseek.com/v1/", "deepseek-v4-pro"),
            {"thinking": {"type": "disabled"}},
        )

    def test_explicit_switch_requires_revision_enabled(self):
        with self.assertRaises(ValueError):
            ResearchConfig(revision_thinking="disabled")
        with self.assertRaises(ValueError):
            ResearchConfig(revise_answer=True, max_answer_chars=100, revision_thinking="typo")
