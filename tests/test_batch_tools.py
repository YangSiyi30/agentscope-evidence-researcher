"""Native same-response requests must respect policy and deliver distinct matching results."""

import copy
import unittest

from agentscope.agent import Agent

from scripts import observe_batch_tools as probe


class BatchToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_requests_consume_only_two_attempts_and_all_results_reach_model(self):
        original = Agent._batch_tool_calls
        run, observation = await probe.observe("search_budget")
        self.assertIs(Agent._batch_tool_calls, original)
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(observation["fixture_model_calls"], 2)
        self.assertEqual(observation["synthetic_http_requests"], 2)
        self.assertEqual(observation["sdk_batches"][0]["type"], "sequential")
        self.assertEqual(len(observation["model_turns"][0]["returned_tool_calls"]), 3)
        self.assertEqual(len(observation["model_turns"][1]["received_tool_results"]), 3)
        self.assertEqual(
            [event["status"] for event in run["trace"]], ["ok", "ok", "blocked_after_stop"]
        )
        self.assertEqual(run["metrics"]["search_attempts"], 2)
        self.assertEqual(len(run["sources"]), 2)

    async def test_first_rate_limit_stops_other_requests_in_same_response(self):
        run, observation = await probe.observe("rate_limit")
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(observation["synthetic_http_requests"], 1)
        self.assertEqual(observation["execution_summary"]["counts"]["blocked_requests"], 2)
        delivered = observation["delivered_observations_by_request"]
        self.assertEqual(delivered[0]["error_code"], "search_rate_limited")
        self.assertEqual([item["remaining_searches"] for item in delivered], [1, 1, 1])
        self.assertEqual(run["metrics"]["status"], "completed")
        self.assertEqual(run["metrics"]["search_stop_reason"], "search_error")
        self.assertEqual(run["sources"], [])
        self.assertEqual(run["metrics"]["queries"], [probe.QUERIES[0]])

    async def test_checker_rejects_missing_result_even_with_complete_trace(self):
        run, observation = await probe.observe("rate_limit")
        turns = copy.deepcopy(observation["model_turns"])
        turns[1]["received_tool_results"].pop()
        checked = probe.inspect(
            run, turns, observation["sdk_batches"], observation["backend_queries"], "rate_limit"
        )
        self.assertEqual(len(run["trace"]), 3)
        self.assertFalse(checked["passed"])
        self.assertFalse(checked["checks"]["all_results_reach_second_model_by_id"])

    async def test_checker_rejects_swapped_evidence_despite_correct_id_set_and_statuses(self):
        run, observation = await probe.observe("search_budget")
        turns = copy.deepcopy(observation["model_turns"])
        first, second = turns[1]["received_tool_results"][:2]
        first["output"], second["output"] = second["output"], first["output"]
        checked = probe.inspect(
            run, turns, observation["sdk_batches"], observation["backend_queries"], "search_budget"
        )
        self.assertTrue(checked["checks"]["all_results_reach_second_model_by_id"])
        self.assertTrue(checked["checks"]["per_call_business_statuses"])
        self.assertFalse(checked["checks"]["per_query_evidence_matches_call_id"])
        self.assertFalse(checked["passed"])
        self.assertEqual(run["metrics"]["search_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
