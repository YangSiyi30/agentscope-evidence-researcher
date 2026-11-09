"""One optional selector improvement: mixed Chinese/API definition scope."""

import unittest

from as_researcher.benchmark import extract_frozen
from as_researcher.segments import definition_focus, parse_document, select_passages


class MixedFocusTests(unittest.TestCase):
    def test_chinese_adjacent_qualified_names_and_backticks(self):
        for text in [
            "asyncio.wait_for取消谁",
            "`asyncio.wait_for()` 等待完成",
            "取消时 await asyncio.wait_for 的条件",
        ]:
            self.assertEqual(definition_focus(text), ("asyncio.wait_for", "exact_definition_mixed"))

    def test_case_pure_identifier_and_underscore_are_preserved(self):
        self.assertEqual(
            definition_focus("asyncio.Timeout"), ("asyncio.Timeout", "exact_definition")
        )
        self.assertEqual(
            definition_focus("wait_for 的条件"), ("wait_for", "exact_definition_mixed")
        )
        self.assertEqual(definition_focus("stdio"), ("stdio", "exact_definition"))

    def test_multiple_apis_or_no_identifier_do_not_guess(self):
        for text in [
            "asyncio.timeout 与 asyncio.Timeout 区别",
            "Streamable HTTP 会话",
            "比较取消机制",
        ]:
            self.assertEqual(definition_focus(text), (None, "page_lexical"))

    def test_repeated_same_api_is_not_ambiguous(self):
        self.assertEqual(
            definition_focus("api.foo 的 api.foo 条件"), ("api.foo", "exact_definition_mixed")
        )

    def test_missing_definition_keeps_lexical_fallback(self):
        result = extract_frozen(
            "<p>api.unknown detail.</p><p>beta condition.</p>",
            "balanced",
            "api.unknown 机制,beta 条件",
            500,
        )
        self.assertEqual(result["focus_allocation"]["facets"][0]["scope"], "page_lexical")
        self.assertIn("detail", result["content"])

    def test_scoping_uses_innermost_case_sensitive_definition(self):
        html = "<dl><dt>api.Foo()</dt><dd><p>UPPER fact.</p><dl><dt>api.Foo.child()</dt><dd><p>CHILD fact.</p></dd></dl></dd><dt>api.foo()</dt><dd><p>lower fact.</p></dd></dl>"
        result = extract_frozen(html, "balanced", "api.foo 机制,api.Foo 机制", 300)
        self.assertTrue(
            all(
                f["scope"] == "exact_definition_mixed" for f in result["focus_allocation"]["facets"]
            )
        )
        self.assertEqual(result["focus_allocation"]["facets"][1]["matched_candidates"], 2)
        self.assertIn("UPPER fact", result["content"])
        self.assertIn("lower fact", result["content"])

    def test_budgets_offsets_and_atomic_blocks_survive_mixed_scope(self):
        html = (
            "<dl><dt>api.foo()</dt><dd><p>foo condition. "
            + "detail " * 80
            + "</p><pre>"
            + "code " * 100
            + "</pre></dd><dt>api.bar()</dt><dd><p>bar condition.</p></dd></dl>"
        )
        document = parse_document(html)
        for budget in [100, 200, 400, 800, 1200]:
            result = select_passages(
                document, "api.foo 取消,api.bar 条件", budget, ranking="balanced"
            )
            self.assertLessEqual(len(result["content"]), budget)
            for row in result["segments"]:
                self.assertEqual(document["text"][row["start"] : row["end"]], row["text"])
                if row["kind"] in {"pre", "code_context"}:
                    self.assertFalse(row["is_truncated"])

    def test_single_focus_does_not_silently_add_another_retrieval_algorithm(self):
        html = "<p>api.foo explicit details.</p>"
        self.assertEqual(
            extract_frozen(html, "balanced", "api.foo 取消", 400)["content"],
            extract_frozen(html, "bm25", "api.foo 取消", 400)["content"],
        )


if __name__ == "__main__":
    unittest.main()
