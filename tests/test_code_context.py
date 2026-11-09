import unittest

from as_researcher.segments import parse_document, select_passages


class CodeContextTests(unittest.TestCase):
    def test_code_hits_do_not_displace_primary_definition_conditions(self):
        doc = parse_document(
            "<dl><dt>wait_for()</dt><dd><p>Wait with a timeout.</p><p>"
            + "Context. " * 30
            + "</p><p>It waits until cancellation completes.</p><p>Example:</p><pre>"
            + "wait_for(work())\n" * 25
            + "</pre><p>Example note.</p></dd></dl>"
        )
        result = select_passages(doc, "wait_for", 800)
        self.assertIn("It waits until cancellation completes.", result["content"])
        self.assertTrue(
            any(
                p["primary_definition"] and "cancellation completes" in p["text"]
                for p in result["segments"]
            )
        )
        self.assertIn("code_context_omitted_for_budget", result["selection_warnings"])

    def test_equivalence_examples_keep_indentation_qualification_and_offsets(self):
        html = (
            "<dl><dt>target()</dt><dd><p>Definition.</p><p>The statement:</p>"
            "<pre><span>task = make_task()</span>\nawait protect(task)</pre>"
            "<p>is equivalent to:</p><pre>try:\n    await work()\nexcept Error:\n    pass</pre>"
            "<p>Except that the caller can still be cancelled.</p></dd></dl>"
        )
        doc = parse_document(html)
        result = select_passages(doc, "target", 800)
        block = next(p for p in result["segments"] if p["kind"] == "code_context")
        self.assertEqual(block["code_block_count"], 2)
        self.assertIn("try:\n    await work()\nexcept Error:\n    pass", block["text"])
        self.assertIn("caller can still be cancelled", block["text"])
        self.assertEqual(block["text"], doc["text"][block["start"] : block["end"]])
        code = [m for m in block["members"] if m["kind"] == "pre"]
        self.assertEqual(
            block["text"][code[0]["start"] : code[0]["end"]],
            "task = make_task()\nawait protect(task)",
        )
        self.assertFalse(block["is_truncated"])

    def test_code_bundle_that_cannot_fit_is_not_cut_into_false_equivalence(self):
        doc = parse_document(
            "<dl><dt>target()</dt><dd><p>The statement:</p><pre>"
            + "x" * 300
            + "</pre><p>is equivalent to:</p><pre>y</pre></dd></dl>"
        )
        small = select_passages(doc, "target", 200)
        self.assertNotIn("The statement:", small["content"])
        self.assertNotIn("is equivalent to:", small["content"])
        self.assertEqual(small["omitted_code_blocks"], 2)
        self.assertEqual(small["omitted_code_contexts"], 1)
        self.assertIn("code_context_omitted_for_budget", small["selection_warnings"])
        large = select_passages(doc, "target", 800)
        self.assertIn("x" * 300, large["content"])
        self.assertEqual(large["omitted_code_blocks"], 0)

    def test_standalone_long_code_is_atomic_but_ordinary_prose_still_clips(self):
        doc = parse_document("<pre>" + "code\n" * 300 + "</pre><p>" + "code " * 400 + "</p>")
        result = select_passages(doc, "code", 300)
        self.assertFalse(any(p["kind"] == "pre" for p in result["segments"]))
        self.assertEqual(result["omitted_code_blocks"], 1)
        self.assertIn("partial_prose_fragment", result["selection_warnings"])
        self.assertLessEqual(len(result["content"]), 300)

    def test_scope_boundaries_prevent_pairing_unrelated_definitions_or_headings(self):
        for html in (
            "<dl><dt>Outer</dt><dd><p>Example:</p><dl><dt>Inner</dt><dd><pre>inner()</pre></dd></dl></dd></dl>",
            "<dl><dt>target</dt><dd><p>Example:</p><h3>Other section</h3><pre>other()</pre></dd></dl>",
            "<p>Example:</p><pre>outside_definition()</pre>",
        ):
            doc = parse_document(html)
            self.assertFalse(any(p["kind"] == "code_context" for p in doc["passages"]))

    def test_inline_markup_br_and_hidden_content_do_not_destroy_code(self):
        doc = parse_document(
            "<pre><code>if ok:<br>    <span>call()</span></code><span hidden>secret</span></pre>"
        )
        self.assertEqual(doc["text"], "if ok:\n    call()")

    def test_long_intro_is_not_forced_into_code_bundle(self):
        doc = parse_document(
            "<dl><dt>target</dt><dd><p>" + "long " * 60 + ":</p><pre>code()</pre></dd></dl>"
        )
        self.assertFalse(any(p["kind"] == "code_context" for p in doc["passages"]))

    def test_chinese_intro_and_shared_budget_remain_deterministic(self):
        doc = parse_document(
            "<dl><dt>测试</dt><dd><p>示例：</p><pre>😀 = 1\n    保留缩进</pre><p>条件说明。</p></dd></dl>"
        )
        for budget in (100, 200, 500):
            result = select_passages(doc, "测试", budget)
            self.assertLessEqual(len(result["content"]), budget)
            self.assertEqual(result, select_passages(doc, "测试", budget))
            for passage in result["segments"]:
                self.assertEqual(passage["text"], doc["text"][passage["start"] : passage["end"]])
