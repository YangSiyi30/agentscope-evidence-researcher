"""A rate-limit result must reach the next model call in the actual AgentScope loop."""

import json
import unittest
from unittest.mock import patch

from scripts import observe_failure_loop as probe


class FailureLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_observation_reaches_model_and_agent_finishes_without_retry(self):
        result, observation = await probe.observe()
        self.assertTrue(observation["passed"], observation["checks"])
        turns = observation["model_turns"]
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["received_tool_results"], [])
        request = turns[0]["returned_tool_calls"][0]
        delivered = turns[1]["received_tool_results"][0]
        self.assertEqual(delivered["id"], request["id"])
        self.assertEqual(delivered["name"], "search_web")
        self.assertEqual(delivered["state"], "success")
        payload = json.loads("".join(block["text"] for block in delivered["output"]))
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_code"], "search_rate_limited")
        self.assertEqual(payload["remaining_searches"], 1)
        self.assertTrue(payload["stop"])
        self.assertEqual(turns[1]["returned_tool_calls"], [])
        self.assertEqual(observation["fixture_model_calls"], 2)
        self.assertEqual(observation["synthetic_http_requests"], 1)
        self.assertEqual(result["metrics"]["status"], "completed")
        self.assertEqual(result["metrics"]["stop_reason"], "search_error")
        self.assertEqual(result["sources"], [])
        self.assertEqual(len(result["trace"]), 1)
        self.assertTrue(result["trace"][0]["backend_attempted"])
        self.assertIn("证据不足", result["answer"])

    async def test_checker_rejects_missing_delivery_even_when_trace_records_rate_limit(self):
        # Mutation control: remove tool results only at the recording boundary.
        # The base fixture still sees the real messages and the run still ends.
        original = probe.RecordingModel._call_api

        async def lose_record(model, *args, **kwargs):
            response = await original(model, *args, **kwargs)
            model.turns[-1]["received_tool_results"] = []
            return response

        with patch.object(probe.RecordingModel, "_call_api", lose_record):
            result, observation = await probe.observe()
        self.assertEqual(result["trace"][0]["error_code"], "search_rate_limited")
        self.assertEqual(result["metrics"]["status"], "completed")
        self.assertFalse(observation["passed"])
        self.assertFalse(observation["checks"]["business_rate_limit_delivered"])
        self.assertEqual(observation["delivered_business_observation"], {})


if __name__ == "__main__":
    unittest.main()
