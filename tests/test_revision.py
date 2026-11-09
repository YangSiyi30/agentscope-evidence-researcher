"""Real AgentScope/SDK parser with mock HTTP; no paid services or semantic gold."""

import asyncio
import contextlib
import copy
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from agentscope.credential import OpenAICredential

from as_researcher.cli import save_run
from as_researcher.core import ResearchConfig, Source
from as_researcher.models import ObservedOpenAIChatModel
from as_researcher.researcher import run_research
from as_researcher.revision import MAX_REVISION_INPUT_CHARS, digest, revision_integrity_errors
from scripts.evaluate_v08 import main as evaluate

logging.getLogger("as").setLevel(logging.CRITICAL)
FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "eval/revision_v08.json").read_text("utf-8")
)


class RevisionTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, variant="valid", enabled=True):
        source = Source("Synthetic Sable", FIXTURE["url"], "Synthetic test; read the page.")
        original = FIXTURE["original_unit"].format(id=source.id) * FIXTURE["original_repeat"]
        candidate = FIXTURE["candidate"].format(id=source.id)
        if variant == "not_needed":
            original = candidate
        if variant == "invalid_original":
            original = original.replace("@page", "")
        if variant == "input_budget":
            original += "padding" * MAX_REVISION_INPUT_CHARS
        candidates = {
            "missing_citation": "Sable v2.1 使用 stdio 时 stdout 不得写日志。",
            "changed_level": candidate.replace("@page", "@snippet"),
            "unknown_citation": candidate.replace(source.id, "SRC-00000000"),
            "untyped_added": candidate + f"[{source.id}]",
            "overlong": original,
            "empty": "",
            "meaning_changed": FIXTURE["meaning_changed_candidate"].format(id=source.id),
        }
        candidate = candidates.get(variant, candidate)
        requests, searches, reads = [], [], []

        async def search(query, limit):
            searches.append(query)
            return [source]

        async def reader(url, limit, focus=""):
            reads.append(url)
            return FIXTURE["evidence"]

        async def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            step = len(requests)
            message = {"role": "assistant", "content": None}
            finish = "stop"
            if step in (1, 2) or (step == 4 and variant == "tool_request"):
                name = "search_web" if step != 2 else "read_page"
                args = {"query": "test"} if name == "search_web" else {"source_id": source.id}
                message["tool_calls"] = [
                    {
                        "id": f"call-{step}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
                finish = "tool_calls"
            elif step == 3:
                message["content"] = original
                if variant == "original_length":
                    finish = "length"
            else:
                if variant == "external_cancel":
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
                if variant == "timeout":
                    await asyncio.sleep(1)
                if variant == "error":
                    raise httpx.ConnectError("secret-must-not-appear")
                message["content"] = candidate
                finish = {"truncated": "length", "unknown_finish": "secret-must-not-appear"}.get(
                    variant, "stop"
                )
            response = {
                "id": f"response-{step}",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            }
            if (variant == "unknown_usage" and step == 4) or (
                variant == "unknown_research_usage" and step == 2
            ):
                response.pop("usage")
            return httpx.Response(200, json=response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

            def model():
                return ObservedOpenAIChatModel(
                    credential=OpenAICredential(
                        api_key="secret-must-not-appear", base_url="https://model.invalid/v1"
                    ),
                    model="fixture",
                    stream=False,
                    max_retries=0,
                    client_kwargs={"http_client": client, "max_retries": 0},
                )

            revision_model = model()
            if variant == "retry_config":
                revision_model.max_retries = 1
            result = await run_research(
                FIXTURE["topic"],
                model(),
                search,
                ResearchConfig(
                    max_searches=1,
                    max_page_reads=1,
                    max_answer_chars=250,
                    revise_answer=enabled,
                    revision_timeout=0.01 if variant == "timeout" else 5,
                ),
                reader=reader,
                revision_model=revision_model,
                mode="mock_http",
            )
        self.assertEqual(len(searches), 1)
        self.assertEqual(len(reads), 1)
        self.assertNotIn("secret-must-not-appear", json.dumps(result))
        if len(requests) == 4:
            self.assertFalse(requests[-1].get("tools"))
            self.assertEqual(requests[-1]["tool_choice"], "none")
            self.assertEqual(requests[-1]["max_tokens"], 1200)
        return result, requests

    async def test_one_revision_saves_original_request_usage_and_candidate(self):
        result, requests = await self.exercise()
        revision = result["revision"]
        self.assertEqual(len(requests), 4)
        self.assertEqual(revision["status"], "applied_checks_only")
        self.assertEqual(result["answer"], revision["candidate_answer"])
        self.assertEqual(result["metrics"]["research_token_usage"], {"input": 60, "output": 30})
        self.assertEqual(result["metrics"]["token_usage"], {"input": 80, "output": 40})
        self.assertEqual(revision["input_sha256"], digest(revision["request"]["user"]))
        self.assertTrue(result["metrics"]["answer_within_limit"])
        self.assertTrue(result["metrics"]["warnings"])
        self.assertEqual(revision_integrity_errors(revision, result["answer"]), [])
        changed = copy.deepcopy(revision)
        changed["candidate_answer"] += "tamper"
        self.assertIn(
            "candidate_hash_mismatch", revision_integrity_errors(changed, result["answer"])
        )
        with tempfile.TemporaryDirectory() as directory:
            saved = save_run(result, Path(directory))
            self.assertEqual(
                (saved / "answer.original.md").read_text("utf-8"), revision["original_answer"]
            )
            self.assertEqual((saved / "answer.candidate.md").read_text("utf-8"), result["answer"])

    async def test_default_off_within_limit_and_incomplete_runs_do_not_call_revision(self):
        result, requests = await self.exercise(enabled=False)
        self.assertEqual(len(requests), 3)
        self.assertEqual(result["revision"]["status"], "disabled")
        for variant, status in (
            ("not_needed", "not_needed"),
            ("original_length", "skipped_incomplete_run"),
        ):
            with self.subTest(variant=variant):
                result, requests = await self.exercise(variant)
                self.assertEqual(len(requests), 3)
                self.assertEqual(result["revision"]["status"], status)

    async def test_bad_candidates_keep_original_without_retrying_or_new_tools(self):
        for variant in (
            "missing_citation",
            "changed_level",
            "unknown_citation",
            "untyped_added",
            "overlong",
            "empty",
            "truncated",
            "unknown_finish",
            "tool_request",
            "timeout",
            "error",
        ):
            with self.subTest(variant=variant):
                result, requests = await self.exercise(variant)
                if variant == "timeout":
                    # The deadline includes SDK formatting, so it can expire
                    # before HTTP starts. Neither outcome may trigger a retry.
                    self.assertIn(len(requests), (3, 4))
                    self.assertIn("revision_timeout", result["revision"]["errors"])
                    self.assertEqual(result["revision"]["attempts"], 1)
                else:
                    self.assertEqual(len(requests), 4)
                self.assertEqual(result["revision"]["status"], "rejected")
                self.assertEqual(result["answer"], result["revision"]["original_answer"])
                self.assertFalse(result["metrics"]["answer_within_limit"])

    async def test_invalid_input_and_retry_config_skip_paid_call(self):
        for variant in ("invalid_original", "input_budget", "retry_config"):
            with self.subTest(variant=variant):
                result, requests = await self.exercise(variant)
                self.assertEqual(len(requests), 3)
                self.assertEqual(result["revision"]["attempts"], 0)
                self.assertEqual(result["revision"]["status"], "skipped")

    async def test_unknown_revision_usage_does_not_become_zero_cost(self):
        result, _ = await self.exercise("unknown_usage")
        self.assertIsNone(result["revision"]["token_usage"])
        self.assertIsNone(result["metrics"]["token_usage"])
        self.assertIsNotNone(result["metrics"]["research_token_usage"])
        result, _ = await self.exercise("unknown_research_usage")
        self.assertIsNone(result["metrics"]["research_token_usage"])
        self.assertIsNone(result["metrics"]["token_usage"])
        self.assertIsNotNone(result["revision"]["token_usage"])

    async def test_same_citations_do_not_prove_negations_and_scope_survived(self):
        result, _ = await self.exercise("meaning_changed")
        self.assertTrue(result["revision"]["applied"])
        self.assertEqual(result["revision"]["semantic_review"], "required_not_automated")
        self.assertTrue(result["metrics"]["warnings"])

    async def test_explicit_model_and_character_budget_required_before_research(self):
        with self.assertRaises(ValueError):
            ResearchConfig(revise_answer=True)
        with self.assertRaises(ValueError):
            await run_research(
                "test", None, None, ResearchConfig(revise_answer=True, max_answer_chars=100)
            )

    async def test_external_cancellation_propagates_without_a_retry(self):
        # The SDK adapter consumes CancelledError. The revision boundary must
        # restore it so a caller's explicit cancel does not look completed.
        task = asyncio.create_task(self.exercise("external_cancel"))
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_experiment_plan_does_not_load_secrets_create_dirs_or_call_models(self):
        for suite in ("omission", "revision"):
            with (
                patch("scripts.evaluate_v08.live_model") as model,
                patch("scripts.evaluate_v08.load_dotenv") as env,
                patch("pathlib.Path.mkdir") as mkdir,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(evaluate(["--suite", suite]), 0)
                model.assert_not_called()
                env.assert_not_called()
                mkdir.assert_not_called()
