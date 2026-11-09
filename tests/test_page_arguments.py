"""Direct page type guards must keep resources intact; native SDK behavior stays separate."""

import copy
import json
import unittest

from as_researcher.core import ResearchConfig
from as_researcher.researcher import ResearchSession
from as_researcher.search import FixtureSearch
from scripts import observe_page_arguments as probe


class PageArgumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_eight_direct_bad_types_are_recorded_and_later_read_succeeds(self):
        observation = await probe.observe_direct()
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(len(observation["rows"]), 8)

    async def test_disabled_reader_keeps_priority_and_safe_fields_for_bad_types(self):
        session = ResearchSession(ResearchConfig(), FixtureSearch())
        response = await session.read_page(["private marker"], {"private marker": True})
        data = json.loads(response.content[0].text)
        self.assertEqual(data["error"], "reader_disabled")
        self.assertEqual(data["source_id"], "")
        self.assertEqual(len(session.trace), 1)
        self.assertEqual(session.page_attempts, 0)
        self.assertEqual(session.trace[0]["focus"], "")
        self.assertNotIn("private marker", json.dumps({"data": data, "trace": session.trace}))

    async def test_native_list_source_is_sdk_rejected_and_valid_read_continues(self):
        run, observation = await probe.observe_native("source_list")
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(len(run["trace"]), 2)

    async def test_native_object_focus_is_sdk_rejected_without_poisoning_read_ids(self):
        run, observation = await probe.observe_native("focus_object")
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(run["metrics"]["page_read_attempts"], 1)
        self.assertEqual(len(run["page_evidence"]), 1)

    async def test_numeric_focus_repair_is_preserved_and_duplicate_is_blocked(self):
        run, observation = await probe.observe_native("focus_number")
        self.assertTrue(observation["passed"], observation["checks"])
        self.assertEqual(observation["reader_calls"][0]["focus"], "123")
        self.assertEqual(run["trace"][-1]["error_type"], "already_attempted")

    async def test_checker_rejects_missing_sdk_result_even_with_valid_page_trace(self):
        run, observation = await probe.observe_native("source_list")
        turns = copy.deepcopy(observation["model_turns"])
        turns[2]["received_tool_results"] = [
            block for block in turns[2]["received_tool_results"] if block["id"] != probe.PAGE_IDS[0]
        ]
        checked = probe.inspect_native(run, turns, observation["reader_calls"], "source_list")
        self.assertEqual(len(run["page_evidence"]), 1)
        self.assertFalse(checked["passed"])
        self.assertFalse(checked["checks"]["both_page_results_reach_third_model_by_id"])


if __name__ == "__main__":
    unittest.main()
