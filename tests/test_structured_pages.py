"""Evidence context, table ambiguity and bounded-selection regression contracts."""

import unittest
from unittest.mock import patch

from as_researcher.pages import PageReader, PageReadError
from as_researcher.segments import parse_document, select_passages


class StructuredPagesTests(unittest.TestCase):
    def test_table_preserves_header_value_relationship_and_inline_text(self):
        doc = parse_document(
            "<h2>Types</h2><table><tr><th>Declared type</th><th>Affinity</th></tr><tr><td><code>VAR</code>CHAR(255)</td><td><p>TEXT</p></td></tr></table><p>After table.</p>"
        )
        result = select_passages(doc, "VARCHAR(255)", 180)
        self.assertIn("Declared type: VARCHAR(255) | Affinity: TEXT", result["content"])
        self.assertEqual(result["segments"][0]["table_mapping"], "header_value")
        self.assertIn("After table.", doc["text"])
        self.assertLessEqual(len(result["content"]), 180)
        for p in result["segments"]:
            self.assertEqual(p["text"], doc["text"][p["start"] : p["end"]])

    def test_table_row_is_never_partially_sliced_to_fit(self):
        html = (
            "<table><tr><th>Name</th><th>Value</th></tr><tr><td>target</td><td>"
            + "x" * 1000
            + "</td></tr></table>"
        )
        doc = parse_document(html)
        short = select_passages(doc, "target", 150)
        self.assertEqual(short["content"], "")
        self.assertEqual(short["omitted_table_rows"], 1)
        self.assertIn("table_rows_omitted_for_budget", short["selection_warnings"])
        long = select_passages(doc, "target", 1300)
        self.assertIn("x" * 1000, long["content"])
        self.assertFalse(long["segments"][0]["is_truncated"])

    def test_complex_rowspan_colspan_nested_and_repeated_headers_are_unmapped(self):
        for body in [
            '<tr><td colspan="2">target</td></tr>',
            '<tr><td rowspan="2">target</td><td>v</td></tr><tr><td>w</td></tr>',
            "<tr><td>target<table><tr><td>nested</td></tr></table></td><td>v</td></tr>",
            "<tr><th>Other</th><th>Headers</th></tr><tr><td>target</td><td>v</td></tr>",
        ]:
            with self.subTest(body=body):
                result = select_passages(
                    parse_document(
                        "<table><tr><th>Name</th><th>Value</th></tr>" + body + "</table>"
                    ),
                    "target",
                    500,
                )
                self.assertIn("target", result["content"])
                self.assertNotIn("Name: target", result["content"])
                self.assertIn("table_column_mapping_unavailable", result["selection_warnings"])

    def test_ragged_or_headerless_rows_do_not_invent_headers(self):
        for html in [
            "<table><tr><th>A</th><th>B</th></tr><tr><td>target</td></tr></table>",
            "<table><tr><td>target</td><td>value</td></tr></table>",
        ]:
            result = select_passages(parse_document(html), "target", 300)
            self.assertIn("[table columns unmapped]", result["content"])
            self.assertNotIn("A: target", result["content"])

    def test_hidden_cells_and_scripts_do_not_enter_visible_table(self):
        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>target<script>secret</script><span hidden>hidden-value</span></td><td>visible</td></tr></table>"
        text = parse_document(html)["text"]
        self.assertNotIn("secret", text)
        self.assertNotIn("hidden-value", text)
        self.assertIn("A: target | B: visible", text)

    def test_multiple_tables_and_unclosed_table_do_not_cross_assign_headers(self):
        doc = parse_document(
            "<table><tr><th>A</th></tr><tr><td>one</td></tr></table><table><tr><td>two</td></tr>"
        )
        self.assertIn("A: one", doc["text"])
        self.assertIn("two", doc["text"])
        self.assertNotIn("A: two", doc["text"])

    def test_adjacent_definition_prose_keeps_qualification_without_repeated_prefix(self):
        html = (
            "<h1>API</h1><dl><dt>wait_for()</dt><dd><p>Wait for a future.</p><p>Cancellation is requested.</p><p>It waits until cancellation completes.</p><pre>"
            + "sample code " * 200
            + "</pre><p>Later note.</p></dd></dl>"
        )
        doc = parse_document(html)
        result = select_passages(doc, "wait_for", 170)
        self.assertIn("until cancellation completes", result["content"])
        body = next(p for p in result["segments"] if "Wait for a future." in p["text"])
        self.assertIn("until cancellation completes", body["text"])
        self.assertFalse(body["is_truncated"])
        self.assertIn("\n\nCancellation", result["content"])

    def test_nested_definitions_keep_own_context_and_restore_outer_scope(self):
        doc = parse_document(
            "<dl><dt>Outer</dt><dd><p>Before.</p><dl><dt>Inner</dt><dd><p>Inside.</p></dd></dl><p>After.</p></dd></dl><p>Outside.</p>"
        )
        inside = next(p for p in doc["passages"] if p["text"] == "Inside.")
        after = next(p for p in doc["passages"] if p["text"] == "After.")
        self.assertEqual(inside["definition_path"], ["Outer", "Inner"])
        self.assertEqual(after["definition_path"], ["Outer"])
        self.assertEqual(doc["passages"][-1]["definition_path"], [])

    def test_long_definition_offsets_and_clipping_warning_remain_consistent(self):
        doc = parse_document(
            "<dl><dt>目标</dt><dd><p>" + "长段😀" * 700 + "</p><p>最后条件。</p></dd></dl>"
        )
        result = select_passages(doc, "目标", 150)
        self.assertIn("partial_prose_fragment", result["selection_warnings"])
        self.assertLessEqual(len(result["content"]), 150)
        for p in result["segments"]:
            self.assertEqual(p["text"], doc["text"][p["start"] : p["end"]])
        self.assertEqual(
            result,
            select_passages(
                parse_document(
                    "<dl><dt>目标</dt><dd><p>" + "长段😀" * 700 + "</p><p>最后条件。</p></dd></dl>"
                ),
                "目标",
                150,
            ),
        )


class EmptySelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_insufficient_budget_is_not_logged_as_successful_page_read(self):
        async def html(url):
            return "<table><tr><th>Name</th></tr><tr><td>target " + "x" * 500 + "</td></tr></table>"

        with patch("as_researcher.pages.fetch_html", html):
            with self.assertRaises(PageReadError) as caught:
                await PageReader()("https://example.org/table", 100, "target")
        self.assertEqual(str(caught.exception), "excerpt_budget_insufficient")
