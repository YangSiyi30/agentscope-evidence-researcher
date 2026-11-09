"""Unrepaired values need safe application observations; SDK binding/repair stay distinct."""

import copy
import json
import unittest

from as_researcher.core import ResearchConfig
from as_researcher.researcher import ResearchSession
from as_researcher.search import FixtureSearch
from scripts import observe_argument_boundary as probe


class ArgumentBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_bad_types_are_recorded_without_backend_or_global_stop(self):
        for field in ("query", "knowledge_gap"):
            for value in (None, 123, ["private marker"], {"private marker": True}):
                with self.subTest(field=field, value_type=type(value).__name__):
                    backend = FixtureSearch()
                    session = ResearchSession(ResearchConfig(max_searches=2), backend)
                    arguments = {"query": "valid query", "knowledge_gap": "valid gap", field: value}
                    chunk = await session.search_web(**arguments)
                    data = json.loads(chunk.content[0].text)
                    self.assertEqual(data["status"], "blocked_invalid_arguments")
                    self.assertFalse(data["stop"])
                    self.assertEqual(data["remaining_searches"], 2)
                    self.assertEqual(session.attempts, 0)
                    self.assertEqual(len(session.trace), 1)
                    self.assertFalse(session.trace[0]["backend_attempted"])
                    self.assertEqual(session.trace[0][field], "")
                    self.assertNotIn("private marker", json.dumps(session.trace))
                    self.assertEqual(backend.calls, [])
                    await session.search_web("later valid query")
                    self.assertEqual(session.attempts, 1)

    async def test_native_unrepaired_query_type_is_sdk_rejected_and_later_search_runs(self):
        run, observation = await probe.observe("query_list")
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(len(run["trace"]), 2)
        self.assertEqual(observation["execution_summary"]["counts"]["blocked_requests"], 0)

    async def test_native_unrepaired_gap_type_is_sdk_rejected_and_later_search_runs(self):
        run, observation = await probe.observe("gap_object")
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(len(run["trace"]), 2)
        self.assertEqual(run["metrics"]["search_attempts"], 2)

    async def test_missing_required_query_is_sdk_error_and_not_application_trace(self):
        run, observation = await probe.observe("missing_query")
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(
            observation["model_turns"][1]["received_tool_results"][1]["state"], "error"
        )
        self.assertEqual(len(run["trace"]), 2)
        self.assertEqual(observation["execution_summary"]["counts"]["blocked_requests"], 0)

    async def test_sdk_number_repair_is_preserved_and_consumes_real_budget(self):
        run, observation = await probe.observe("number_repair")
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(observation["backend_queries"], [probe.FIRST, "123"])
        self.assertEqual(run["trace"][2]["status"], "blocked_after_stop")

    async def test_checker_rejects_missing_sdk_result_despite_valid_application_trace(self):
        run, observation = await probe.observe("query_list")
        turns = copy.deepcopy(observation["model_turns"])
        turns[1]["received_tool_results"] = [
            b for b in turns[1]["received_tool_results"] if b["id"] != probe.IDS[1]
        ]
        checked = probe.inspect(run, turns, observation["backend_queries"], "query_list")
        self.assertEqual([event["status"] for event in run["trace"]], ["ok", "ok"])
        self.assertFalse(checked["passed"])
        self.assertFalse(checked["checks"]["all_three_results_delivered_by_id"])

    async def test_existing_search_stop_takes_precedence_over_bad_argument_types(self):
        session = ResearchSession(ResearchConfig(max_searches=2), FixtureSearch("search_error"))
        await session.search_web("first failure")
        chunk = await session.search_web(["invalid after stop"])
        self.assertEqual(json.loads(chunk.content[0].text)["status"], "blocked_after_stop")
        self.assertEqual(session.stop_reason, "search_error")
        self.assertEqual(session.attempts, 1)
        self.assertFalse(session.trace[-1]["backend_attempted"])


if __name__ == "__main__":
    unittest.main()
