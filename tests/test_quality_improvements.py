"""Adversarial boundary cases for coverage, composition and review experiments."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from as_researcher.benchmark import (
    check_blind_review,
    digest,
    extract_frozen,
    prepare_blind_review,
    review_agreement,
    source_digest,
    summarize_trials,
)
from as_researcher.core import ResearchConfig
from as_researcher.evidence import page_observation, reader_page_data
from as_researcher.researcher import compact_answer_prompt
from as_researcher.review import fingerprint
from as_researcher.segments import focus_facets, parse_document, select_passages
from scripts.benchmark_research import main
from tests.test_benchmark import example_run, task


class BalancedTests(unittest.TestCase):
    def test_explicit_facets_keep_case_deduplicate_and_bound(self):
        self.assertEqual(focus_facets("Foo, Foo；foo，bar;ignored"), ["Foo", "foo", "bar"])

    def test_two_distant_definitions_both_receive_budget(self):
        document = parse_document(
            "<dl><dt>alpha()</dt><dd><p>alpha condition A. "
            + "alpha detail. " * 60
            + "</p></dd><dt>beta()</dt><dd><p>beta condition B. "
            + "beta detail. " * 60
            + "</p></dd></dl>"
        )
        for limit in (200, 800, 2400, 5000):
            with self.subTest(limit=limit):
                result = select_passages(document, "alpha,beta", limit, ranking="balanced")
                self.assertLessEqual(len(result["content"]), limit)
                self.assertIn("alpha condition A", result["content"])
                self.assertIn("beta condition B", result["content"])
                self.assertEqual(
                    len({p["segment_id"] for p in result["segments"]}), len(result["segments"])
                )
                for p in result["segments"]:
                    self.assertEqual(document["text"][p["start"] : p["end"]], p["text"])

    def test_exact_api_scope_preserves_case(self):
        html = "<dl><dt>api.Foo()</dt><dd><p>UPPER FACT.</p></dd><dt>api.foo()</dt><dd><p>lower fact.</p></dd></dl>"
        result = extract_frozen(html, "balanced", "api.foo,api.Foo", 800)
        self.assertTrue(
            all(r["scope"] == "exact_definition" for r in result["focus_allocation"]["facets"])
        )
        self.assertIn("UPPER FACT", result["content"])
        self.assertIn("lower fact", result["content"])

    def test_empty_or_single_focus_preserves_bm25_fallback(self):
        for focus in ("", "alpha", "alpha,alpha"):
            a = extract_frozen("<p>alpha facts</p>", "balanced", focus, 300)
            b = extract_frozen("<p>alpha facts</p>", "bm25", focus, 300)
            self.assertEqual(a["content"], b["content"])

    def test_atomic_code_not_truncated_to_force_facet_coverage(self):
        html = (
            "<dl><dt>alpha()</dt><dd><pre>alpha "
            + "x " * 300
            + "</pre></dd><dt>beta()</dt><dd><p>beta fact.</p></dd></dl>"
        )
        result = extract_frozen(html, "balanced", "alpha,beta", 300)
        self.assertIn("beta fact", result["content"])
        self.assertNotIn("x x", result["content"])
        self.assertGreater(result["omitted_code_blocks"], 0)

    def test_absent_focus_reported_not_fabricated(self):
        result = extract_frozen("<p>alpha real fact.</p>", "balanced", "alpha,zzzz_missing", 300)
        self.assertIn("focus_no_lexical_match", result["selection_warnings"])
        self.assertEqual(result["focus_allocation"]["facets"][1]["matched_candidates"], 0)

    def test_reader_metadata_roundtrip_keeps_allocation(self):
        result = extract_frozen("<p>alpha beta fact.</p>", "balanced", "alpha,beta", 300)
        saved = reader_page_data(result)
        self.assertEqual(page_observation(saved)["focus_allocation"], result["focus_allocation"])
        with self.assertRaises(ValueError):
            reader_page_data({"content": "x", "focus_allocation": []})

    def test_unicode_whole_code_and_table_boundaries(self):
        html = "<h2>超时</h2><p>取消条件说明。</p><table><tr><th>接口</th><th>行为</th></tr><tr><td>甲</td><td>取消调用者</td></tr></table><pre>乙 = '保留完整代码'</pre>"
        d = parse_document(html)
        for limit in (100, 200, 500):
            r = select_passages(d, "甲,乙,取消", limit, ranking="balanced")
            self.assertLessEqual(len(r["content"]), limit)
            self.assertTrue(
                all(
                    not p["is_truncated"]
                    for p in r["segments"]
                    if p["kind"].startswith("table") or p["kind"] in {"pre", "code_context"}
                )
            )

    def test_refill_counts_changed_prefix_overhead(self):
        html = (
            "<h1>"
            + "Long ancestor " * 20
            + "</h1><h2>"
            + "deep heading " * 10
            + "</h2><p>alpha beta "
            + "detail " * 200
            + "</p><p>beta other "
            + "detail " * 150
            + "</p>"
        )
        for budget in range(100, 1200, 13):
            result = extract_frozen(html, "balanced", "alpha,beta,missing", budget)
            self.assertLessEqual(len(result["content"]), budget)

    def test_compact_prefix_retains_full_heading_metadata(self):
        html = "<h1>Root</h1><h2>Section</h2><dl><dt>alpha()</dt><dd><p>alpha fact.</p></dd><dt>beta()</dt><dd><p>beta fact.</p></dd></dl>"
        result = extract_frozen(html, "balanced", "alpha,beta", 500)
        self.assertNotIn("Root / Section", result["content"])
        self.assertTrue(
            all(p["heading_path"][:2] == ["Root", "Section"] for p in result["segments"])
        )


class ExperimentPlanTests(unittest.TestCase):
    def plan(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["plan", *args]), 0)
        return json.loads(output.getvalue())

    def test_answer_styles_have_distinct_trials_and_counterbalanced_order(self):
        plan = self.plan(
            "--strategy", "balanced", "--answer-style", "standard", "--answer-style", "compact"
        )
        self.assertEqual(
            [r["answer_style"] for r in plan["trials"]],
            ["standard", "compact", "compact", "standard"],
        )
        self.assertEqual(len({r["id"] for r in plan["trials"]}), 4)

    def test_two_axes_rejected_before_network_or_execution(self):
        with patch("scripts.benchmark_research.execute") as execute, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["plan", "--answer-style", "standard", "--answer-style", "compact"])
            execute.assert_not_called()

    def test_revision_is_explicit_plan_does_not_call_model(self):
        with patch("as_researcher.models.live_model") as model:
            plan = self.plan("--revise-answer", "--strategy", "segments")
        model.assert_not_called()
        self.assertTrue(plan["revise_answer"])
        self.assertEqual(plan["model_retries"], 0)

    def test_compact_style_no_auto_truncation_and_default_unchanged(self):
        self.assertEqual(ResearchConfig().answer_style, "standard")
        self.assertIn("375", compact_answer_prompt(500))
        self.assertIn("必需", compact_answer_prompt(None))

    def test_summary_does_not_merge_style_variants(self):
        rows = [
            {
                "strategy": "segments",
                "variant": s,
                "task_id": "t",
                "repeat": 1,
                "assessment": {"passed": success},
            }
            for s, success in (("segments__standard", False), ("segments__compact", True))
        ]
        result = summarize_trials(rows)
        self.assertEqual(len(result["strategies"]), 2)
        self.assertEqual(result["pairs"][0]["outcome"], "right_wins")


class BlindReviewTests(unittest.TestCase):
    def setUp(self):
        self.template, self.mapping = prepare_blind_review([(task(), example_run())])

    def judged(self, values, name):
        packet = copy.deepcopy(self.template)
        packet["reviewer"] = {"kind": "assistant", "name": name}
        for row, value in zip(packet["items"][0]["labels"], values):
            row.update(met=value, reason="Synthetic regression label" if value is not None else "")
        return packet

    def test_export_hides_config_and_never_claims_human_review(self):
        run = example_run()
        run.update(excerpt_strategy="balanced", config={"answer_style": "compact"})
        packet, mapping = prepare_blind_review([(task(), run)])
        self.assertNotIn("balanced", json.dumps(packet))
        self.assertNotIn("compact", json.dumps(packet))
        self.assertEqual(packet["reviewer"]["kind"], "unassigned")
        self.assertEqual(len(mapping["bindings"]), 1)

    def test_unassigned_template_is_valid_incomplete(self):
        result = check_blind_review(self.template, self.template)
        self.assertTrue(result["valid"])
        self.assertFalse(result["complete"])

    def test_changed_evidence_answer_or_criterion_rejected(self):
        for field in ("answer", "evidence", "rubric"):
            packet = self.judged([True, False], "a")
            packet["items"][0][field] = "tampered"
            self.assertFalse(check_blind_review(self.template, packet)["valid"])

    def test_integer_label_or_missing_reason_rejected(self):
        packet = self.judged([True, False], "a")
        packet["items"][0]["labels"][0]["met"] = 1
        self.assertFalse(check_blind_review(self.template, packet)["valid"])
        packet = self.judged([True, False], "a")
        packet["items"][0]["labels"][0]["reason"] = " "
        self.assertFalse(check_blind_review(self.template, packet)["valid"])

    def test_kappa_and_null_denominator(self):
        result = review_agreement(
            self.template, self.judged([True, False], "a"), self.judged([True, True], "b")
        )
        self.assertEqual(result["jointly_judged"], 2)
        self.assertEqual(result["agreement"], 0.5)
        self.assertEqual(result["cohen_kappa"], 0)
        result = review_agreement(
            self.template, self.judged([True, None], "a"), self.judged([True, False], "b")
        )
        self.assertEqual(result["jointly_judged"], 1)
        self.assertEqual(result["unpaired"], 1)
        self.assertIsNone(result["cohen_kappa"])
        self.assertFalse(result["complete"])

    def test_no_joint_labels_and_same_reviewer_not_certified(self):
        result = review_agreement(self.template, self.template, self.template)
        self.assertIsNone(result["agreement"])
        self.assertFalse(result["declared_distinct_reviewers"])

    def test_disagreement_locators_retained(self):
        result = review_agreement(
            self.template, self.judged([True, False], "a"), self.judged([False, False], "b")
        )
        self.assertEqual(len(result["disagreements"]), 1)

    def test_cli_blind_export_pending_and_tampered_template(self):
        from tests.test_benchmark import ROOT

        suite_file = ROOT / "eval/research_suite.json"
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as directory:
            batch = Path(directory) / "batch"
            folder = batch / "trial" / "record"
            folder.mkdir(parents=True)
            run = example_run()
            run.update(
                benchmark={"trial_id": "trial", "suite_sha256": digest(suite_file.read_bytes())},
                provenance={"source_sha256": source_digest()},
                excerpt_strategy="segments",
            )
            trial = {
                "id": "trial",
                "task_id": "mcp_stdio",
                "repeat": 1,
                "strategy": "segments",
                "status": "saved",
                "run_file": "trial/record",
                "run_sha256": fingerprint(run),
            }
            (batch / "suite.json").write_bytes(suite_file.read_bytes())
            for name, value in (
                (
                    "plan.json",
                    {
                        "suite_sha256": digest(suite_file.read_bytes()),
                        "source_sha256": source_digest(),
                        "trials": [trial],
                    },
                ),
                ("index.json", [trial]),
            ):
                (batch / name).write_text(json.dumps(value), encoding="utf-8")
            (folder / "run.json").write_text(json.dumps(run), encoding="utf-8")
            bundle = Path(directory) / "blind"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["blind-prepare", "--batch", str(batch), "--output", str(bundle)]), 0
                )
                template = bundle / "reviewer/template.json"
                args = [
                    "blind-check",
                    "--bundle",
                    str(bundle),
                    "--review",
                    str(template),
                    "--review",
                    str(template),
                ]
                self.assertEqual(main(args), 3)
            altered = json.loads(template.read_bytes())
            altered["items"][0]["answer"] = "forged answer"
            template.write_text(json.dumps(altered), encoding="utf-8")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)


if __name__ == "__main__":
    unittest.main()
