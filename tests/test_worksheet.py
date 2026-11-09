import copy
import unittest

from as_researcher.atomic_review import prepare_atomic_review
from as_researcher.worksheet import render_worksheet


class WorksheetTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            "answer": "事实 [SRC-12345678@snippet]",
            "sources": [
                {
                    "id": "SRC-12345678",
                    "url": "https://example.org",
                    "content": "```\n# injected\n<script>bad()</script>",
                }
            ],
        }
        self.packet = prepare_atomic_review(self.run)

    def test_evidence_stays_literal_and_no_labels_or_run_are_changed(self):
        before = copy.deepcopy((self.run, self.packet))
        output = render_worksheet(self.run, self.packet)
        self.assertIn("````text\n```\n# injected", output)
        self.assertIn("SRC-12345678@snippet", output)
        self.assertEqual((self.run, self.packet), before)
        self.assertEqual(self.packet["units"][0]["claims"][0]["label"], "pending")

    def test_mismatched_run_is_rejected(self):
        self.run["answer"] += "changed"
        with self.assertRaises(ValueError):
            render_worksheet(self.run, self.packet)
