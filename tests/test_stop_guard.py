"""A noncompliant fixed model must not bypass the application's search stop guard."""

import copy
import json
import unittest

from scripts import observe_stop_guard as probe


class StopGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_extra_query_is_blocked_and_result_reaches_model(self):
        run, observation = await probe.observe("rate_limit")
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(observation["fixture_model_calls"], 3)
        self.assertEqual(observation["synthetic_http_requests"], 1)
        self.assertEqual(
            [event["status"] for event in run["trace"]], ["error", "blocked_after_stop"]
        )
        first, refusal = observation["delivered_observations"]
        self.assertEqual(first["error_code"], "search_rate_limited")
        self.assertEqual(refusal["remaining_searches"], 1)
        self.assertEqual(refusal["search_attempts"], 1)
        self.assertEqual(observation["execution_summary"]["counts"]["blocked_requests"], 1)
        extra = observation["model_turns"][1]["returned_tool_calls"][0]
        self.assertNotEqual(json.loads(extra["input"])["query"], run["metrics"]["queries"][0])
        self.assertTrue(observation["model_turns"][1]["received_tool_results"])
        self.assertFalse(run["trace"][1]["backend_attempted"])

    async def test_exhausted_search_budget_still_allows_reading_known_source(self):
        run, observation = await probe.observe("search_budget")
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(observation["fixture_model_calls"], 4)
        self.assertEqual(observation["fixture_page_calls"], 1)
        self.assertEqual(run["metrics"]["stop_reason"], "search_budget")
        self.assertEqual(
            [event["status"] for event in run["trace"]], ["ok", "blocked_after_stop", "ok"]
        )
        self.assertEqual(
            [event["backend_attempted"] for event in run["trace"]], [True, False, True]
        )
        self.assertEqual(observation["delivered_observations"][1]["remaining_searches"], 0)
        self.assertEqual(run["page_evidence"][0]["id"], run["sources"][0]["id"])
        self.assertIn(f"[{run['sources'][0]['id']}@page]", run["answer"])
        self.assertEqual(run["metrics"]["warnings"], [])

    async def test_checker_rejects_missing_refusal_despite_intact_trace(self):
        run, observation = await probe.observe("rate_limit")
        turns = copy.deepcopy(observation["model_turns"])
        turns[2]["received_tool_results"] = [
            block for block in turns[2]["received_tool_results"] if block["id"] != "stop-extra"
        ]
        checked = probe.inspect(run, turns, 1, 0, "rate_limit")
        self.assertEqual(run["trace"][1]["status"], "blocked_after_stop")
        self.assertFalse(checked["passed"])
        self.assertFalse(checked["checks"]["refusal_reaches_third_model_call"])

    async def test_checker_rejects_refusal_with_wrong_call_id(self):
        run, observation = await probe.observe("rate_limit")
        turns = copy.deepcopy(observation["model_turns"])
        for block in turns[2]["received_tool_results"]:
            if block["id"] == "stop-extra":
                block["id"] = "unrelated-call"
        checked = probe.inspect(run, turns, 1, 0, "rate_limit")
        self.assertFalse(checked["passed"])
        self.assertFalse(checked["checks"]["matching_requests_and_next_model_results"])
        self.assertEqual(run["metrics"]["search_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
