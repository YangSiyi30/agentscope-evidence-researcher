import unittest

from scripts.evaluate_pages import compare, target_checks


class PageEvaluationTests(unittest.TestCase):
    def test_absent_predeclared_target_is_unscored_for_every_strategy(self):
        rows = compare(
            {"id": "missing", "focus": "predicate", "targets": ["predicate", "谓词"]},
            "<p>predicate means a callable.</p>",
            [100, 800],
        )
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r["all_targets_hit"] is None and not r["target_in_page"] for r in rows))

    def test_far_apart_tokens_are_not_nearby_evidence(self):
        result = target_checks("target" + "x" * 501 + "result", ["target", "result"])
        self.assertTrue(result["all_targets_hit"])
        self.assertFalse(result["targets_within_500_chars"])
        self.assertTrue(
            target_checks("target  result", ["RESULT", "TARGET"])["targets_within_500_chars"]
        )

    def test_repeated_term_does_not_hide_nearby_match(self):
        result = target_checks("target" + "x" * 501 + "target result", ["target", "result"])
        self.assertTrue(result["targets_within_500_chars"])
