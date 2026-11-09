import logging
import unittest

from scripts.probe_mcp_exit import run_probe

logging.getLogger("as").setLevel(logging.CRITICAL)


class MCPProcessExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_child_exit_is_recorded_and_process_is_reaped(self):
        result = await run_probe()
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["children"][0]["exit_code"], 23)
        self.assertLess(result["duration_ms"], 10000)

    async def test_cancel_pending_call_records_cancellation_and_reaps_child(self):
        result = await run_probe("cancel")
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["call_error_type"], "TimeoutError")
        self.assertEqual(result["calls"][-1]["status"], "cancelled")
        self.assertLess(result["duration_ms"], 10000)
