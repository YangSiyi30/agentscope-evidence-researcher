"""Evaluation must not turn unknown, invalid or missing outcomes into successes."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from as_researcher.benchmark import (
    FrozenReader,
    ResearchSuite,
    ResearchTask,
    check_task_review,
    digest,
    extract_frozen,
    load_corpus,
    load_suite,
    operational_checks,
    prepare_task_review,
    summarize_trials,
    trial_assessment,
)
from as_researcher.core import ResearchConfig, Source
from as_researcher.pages import PageReader, TextExtractor
from as_researcher.researcher import ResearchSession
from as_researcher.search import FixtureSearch
from as_researcher.segments import bm25_scores, parse_document, retrieval_tokens, select_passages
from scripts.benchmark_research import execute, main, make_plan, score

ROOT = Path(__file__).resolve().parents[1]


def task():
    return ResearchTask.model_validate(
        {
            "id": "stdio",
            "category": "protocol",
            "question": "Explain the stdout message boundary.",
            "documents": ["mcp"],
            "rubric": [
                {
                    "id": "fact",
                    "dimension": "support",
                    "description": "Valid messages only; retain scope.",
                    "improvement": "Read the relevant protocol clause.",
                },
                {
                    "id": "style",
                    "dimension": "presentation",
                    "description": "Answer clearly without repetition.",
                    "improvement": "Remove repeated sentences.",
                    "required": False,
                },
            ],
        }
    )


def example_run():
    return {
        "answer": "仅有效消息。[SRC-12345678@page]",
        "sources": [],
        "page_evidence": [{"id": "SRC-12345678", "content": "Only valid messages on stdout."}],
        "config": {"max_searches": 0, "max_page_reads": 1},
        "trace": [{"tool": "read_page"}],
        "metrics": {
            "status": "completed",
            "answer_within_limit": True,
            "search_attempts": 0,
            "page_read_attempts": 1,
            "duration_ms": 10,
            "token_usage": None,
            "citations": {"invalid_ids": [], "has_citations": True},
            "evidence_levels": {
                "invalid_references": [],
                "unavailable_references": [],
                "untyped_references": [],
            },
        },
    }


def reviewed(run, case):
    packet = prepare_task_review(run, case)
    packet.update(reviewer={"kind": "assistant", "name": "test-fixture"}, coverage_checked=True)
    for row in packet["criteria"]:
        row.update(
            met=True, answer_quote=run["answer"], reason="Synthetic test label, not real judgment."
        )
    packet["criteria"][0]["evidence"] = [
        {"source_id": "SRC-12345678", "kind": "page", "quote": "Only valid messages"}
    ]
    return packet


class BM25Tests(unittest.TestCase):
    def test_template_payload_cannot_pollute_headings_or_evidence(self):
        html = "<h1>Real heading</h1><p>Visible stdout rule.</p><template><h2>Forged heading</h2><p>stdout _jsx hidden build payload</p><template><p>nested payload</p></template></template><p>Visible ending.</p>"
        document = parse_document(html)
        self.assertNotIn("payload", document["text"])
        self.assertEqual(document["passages"][-1]["heading_path"], ["Real heading"])
        for strategy in ("head", "window", "segments", "bm25"):
            result = extract_frozen(html, strategy, "stdout", 500)
            self.assertIn("Visible stdout rule", result["content"])
            self.assertNotIn("_jsx", result["content"])
            self.assertNotIn("Forged", result["content"])

    def test_template_inside_table_is_not_a_visible_cell(self):
        document = parse_document(
            "<table><tr><th>Key</th></tr><tr><td>real<template><b>hidden</b></template></td></tr></table>"
        )
        self.assertIn("Key: real", document["text"])
        self.assertNotIn("hidden", document["text"])

    def test_plain_extractor_resumes_after_nested_hidden_payload(self):
        parser = TextExtractor()
        parser.feed(
            "<p>before</p><template><script>secret()</script><template>nested</template>data</template><p>after</p>"
        )
        self.assertEqual("".join(parser.parts).strip(), "before\n\nafter")

    def test_long_focus_can_retrieve_late_evidence_that_literal_fallback_misses(self):
        document = parse_document(
            "<p>"
            + "Unrelated introduction. " * 120
            + "</p><h2>wait_for</h2><p>wait_for waits for cancellation completion.</p>"
        )
        focus = "When does wait_for actually finish after cancellation"
        self.assertNotIn("completion", select_passages(document, focus, 230)["content"])
        result = select_passages(document, focus, 230, ranking="bm25")
        self.assertIn("completion", result["content"])
        self.assertEqual(result["selector_version"], "bm25-v1")
        self.assertLessEqual(len(result["content"]), 230)

    def test_identifier_parts_cjk_and_case_are_searchable(self):
        tokens = retrieval_tokens("ASYNCIO.wait_for 取消完成")
        self.assertTrue(
            {"asyncio.wait_for", "wait_for", "wait", "取消", "消完", "完成"}.issubset(tokens)
        )

    def test_repeating_query_terms_cannot_boost_their_weight(self):
        passages = parse_document("<p>alpha beta</p><p>alpha</p>")["passages"]
        self.assertEqual(
            bm25_scores(passages, "alpha beta"), bm25_scores(passages, "alpha alpha beta beta")
        )

    def test_no_match_or_empty_query_falls_back_stably(self):
        document = parse_document("<p>First observation.</p><p>Second observation.</p>")
        a = select_passages(document, "", 200, ranking="bm25")
        b = select_passages(document, "missing", 200, ranking="bm25")
        self.assertEqual(a["content"], b["content"])
        self.assertTrue(a["content"].startswith("[SEG-"))
        self.assertIn("First observation", a["content"])

    def test_offsets_and_atomic_code_survive_alternate_ranking(self):
        document = parse_document(
            "<dl><dt>target()</dt><dd><p>Short definition.</p><p>Example:</p><pre>"
            + "target(x)\n" * 80
            + "</pre><p>Important exception.</p></dd></dl>"
        )
        result = select_passages(document, "target", 300, ranking="bm25")
        self.assertEqual(result["omitted_code_blocks"], 1)
        self.assertFalse(any(p["kind"] in {"pre", "code_context"} for p in result["segments"]))
        for segment in result["segments"]:
            self.assertEqual(segment["text"], document["text"][segment["start"] : segment["end"]])

    def test_rectangular_table_retains_header_mapping(self):
        result = extract_frozen(
            "<table><tr><th>Name</th><th>Value</th></tr><tr><td>target</td><td>three</td></tr></table>",
            "bm25",
            "What is the target value",
            300,
        )
        self.assertIn("Name: target | Value: three", result["content"])

    def test_unknown_ranking_is_rejected(self):
        with self.assertRaises(ValueError):
            select_passages(parse_document("<p>x</p>"), "x", 100, ranking="typo")


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_report_labels_do_not_rewrite_renderer5_history(self):
        from as_researcher.models import ScriptedModel
        from as_researcher.researcher import render_report, run_research

        url = "https://example.org/known"
        run = await run_research(
            "Read the supplied source.",
            ScriptedModel(),
            FixtureSearch(),
            ResearchConfig(max_searches=0, max_page_reads=1),
            reader=FrozenReader({url: "<p>Known evidence.</p>"}, "bm25"),
            source_urls=[url],
            mode="offline_scripted_snapshot",
        )
        self.assertEqual(run["report_renderer_version"], 6)
        self.assertIn("固定模型＋冻结网页快照", render_report(run))
        run["mode"] = "live_model_snapshot"
        self.assertIn("真实模型＋冻结网页快照", render_report(run))
        run["report_renderer_version"] = 5
        self.assertIn("运行标签未识别", render_report(run))

    async def test_actual_reader_and_session_keep_strategy_metadata(self):
        source = Source("test", "https://example.org/page", "")
        with patch(
            "as_researcher.pages.fetch_html",
            AsyncMock(return_value="<p>stdout carries valid messages.</p>"),
        ):
            session = ResearchSession(
                ResearchConfig(max_page_reads=1), FixtureSearch(), PageReader(strategy="bm25")
            )
            session.sources[source.url] = source
            await session.read_page(source.id, "Explain valid stdout messages")
        self.assertEqual(session.page_evidence[source.id]["selector_version"], "bm25-v1")

    async def test_frozen_reader_never_falls_back_to_network(self):
        reader = FrozenReader({"https://example.org/known": "<p>known</p>"}, "bm25")
        with patch("as_researcher.pages.fetch_html") as fetch:
            with self.assertRaises(ValueError):
                await reader("https://example.org/unknown", 100)
            fetch.assert_not_called()


class ReviewTests(unittest.TestCase):
    def test_injection_test_requires_actual_tool_visible_attack(self):
        run = example_run()
        case = task().model_copy(update={"required_exposure": "ATTACK_MARKER_123"})
        self.assertFalse(operational_checks(run, case)["checks"]["adversarial_payload_exposed"])
        run["page_evidence"][0]["content"] += " ATTACK_MARKER_123"
        self.assertTrue(operational_checks(run, case)["passed"])
        run["answer"] += " ATTACK_MARKER_123"
        self.assertTrue(operational_checks(run, case)["adversarial_marker_in_answer"])
        self.assertTrue(operational_checks(run, case)["passed"])

    def test_question_limit_is_checked_even_if_cli_limit_is_looser(self):
        run = example_run()
        run["answer"] = "x" * 501
        result = operational_checks(run, task())
        self.assertTrue(result["checks"]["answer_within_limit"])
        self.assertFalse(result["checks"]["task_answer_within_limit"])
        self.assertFalse(result["passed"])

    def test_pending_is_valid_but_never_a_quality_success(self):
        run, case = example_run(), task()
        assessment = trial_assessment(run, case, prepare_task_review(run, case))
        self.assertTrue(assessment["quality"]["valid"])
        self.assertFalse(assessment["quality"]["complete"])
        self.assertIsNone(assessment["passed"])

    def test_assistant_review_identity_and_quotes_are_kept(self):
        run, case = example_run(), task()
        result = check_task_review(run, case, reviewed(run, case))
        self.assertTrue(result["complete"])
        self.assertEqual(result["reviewer_kind"], "assistant")

    def test_run_and_task_changes_invalidate_old_review(self):
        run, case = example_run(), task()
        packet = reviewed(run, case)
        changed = copy.deepcopy(run)
        changed["answer"] += " extra"
        self.assertFalse(check_task_review(changed, case, packet)["valid"])
        other = case.model_copy(update={"question": "A different question about stdout."})
        self.assertFalse(check_task_review(run, other, packet)["valid"])

    def test_quote_in_another_field_or_level_cannot_support_a_label(self):
        run, case = example_run(), task()
        packet = reviewed(run, case)
        packet["criteria"][0]["evidence"][0]["kind"] = "snippet"
        self.assertFalse(check_task_review(run, case, packet)["valid"])

    def test_bad_positive_label_and_missing_evidence_are_rejected(self):
        run, case = example_run(), task()
        for mutation in (
            lambda p: p["criteria"][0].update(met=1),
            lambda p: p["criteria"][0].update(evidence=[]),
            lambda p: p["criteria"][0].update(answer_quote="invented"),
            lambda p: p.update(coverage_checked=False),
        ):
            packet = reviewed(run, case)
            mutation(packet)
            self.assertIsNone(check_task_review(run, case, packet)["semantic_pass"])

    def test_optional_style_failure_does_not_fail_required_facts(self):
        run, case = example_run(), task()
        packet = reviewed(run, case)
        packet["criteria"][1]["met"] = False
        self.assertTrue(check_task_review(run, case, packet)["semantic_pass"])

    def test_required_failure_produces_specific_improvement(self):
        run, case = example_run(), task()
        packet = reviewed(run, case)
        packet["criteria"][0]["met"] = False
        result = check_task_review(run, case, packet)
        self.assertFalse(result["semantic_pass"])
        self.assertEqual(result["improvements"], [case.rubric[0].improvement])

    def test_completed_answer_can_still_fail_resource_or_reference_gate(self):
        run, case = example_run(), task()
        run["metrics"]["page_read_attempts"] = 2
        self.assertFalse(operational_checks(run, case)["passed"])
        run["metrics"]["page_read_attempts"] = 1
        run["metrics"]["evidence_levels"]["unavailable_references"] = ["SRC-12345678@snippet"]
        self.assertFalse(trial_assessment(run, case, reviewed(run, case))["passed"])


class SummaryTests(unittest.TestCase):
    def test_unrun_and_unreviewed_stay_in_denominator_and_unknown_usage(self):
        rows = [
            {
                "strategy": "segments",
                "task_id": "a",
                "repeat": 1,
                "metrics": {"duration_ms": 10, "token_usage": {"input": 2, "output": 1}},
                "assessment": {"passed": True},
            },
            {
                "strategy": "segments",
                "task_id": "a",
                "repeat": 2,
                "metrics": {"duration_ms": 20, "token_usage": None},
                "assessment": {"passed": False},
            },
            {"strategy": "segments", "task_id": "b", "repeat": 1},
        ]
        value = summarize_trials(rows)["strategies"]["segments"]
        self.assertEqual(value["success_rate_bounds"], [1 / 3, 2 / 3])
        self.assertEqual(value["usage_unknown_or_no_run"], 2)
        self.assertEqual(value["known_token_subtotal"], {"input": 2, "output": 1})
        self.assertIsNone(value["p95_duration_ms"])
        self.assertIsNone(value["empirical_tasks"]["b"]["any_success"])
        self.assertFalse(value["empirical_tasks"]["a"]["all_success"])

    def test_pairs_require_both_outcomes(self):
        rows = [
            {"strategy": s, "task_id": "a", "repeat": 1, "assessment": {"passed": p}}
            for s, p in [("segments", False), ("bm25", True)]
        ]
        self.assertEqual(summarize_trials(rows)["pairs"][0]["outcome"], "right_wins")
        rows[1]["assessment"]["passed"] = None
        self.assertEqual(summarize_trials(rows)["pairs"][0]["outcome"], "unassessed")


class BenchmarkCommandTests(unittest.TestCase):
    def test_saved_completed_refusal_does_not_hide_search_infrastructure_failure(self):
        suite = load_suite(ROOT / "eval/research_suite.json")
        args = SimpleNamespace(
            case=["mcp_stdio"],
            strategy=["segments"],
            mode="search",
            transport="local",
            corpus=None,
            operation="run",
            repeats=1,
            suite=ROOT / "eval/research_suite.json",
            chars_per_page=800,
            answer_chars=700,
            timeout=30,
            model_max_tokens=1800,
            model_thinking="provider-default",
            search_provider="duckduckgo",
        )
        plan = make_plan(args, suite)
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as directory:
            args.output = Path(directory) / "batch"

            def child(command, **kwargs):
                target = Path(command[3]) / (command[4] + ".result.json")
                target.write_text(
                    json.dumps(
                        {"metrics": {"status": "completed"}, "infrastructure_failure": True}
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0)

            with (
                patch("scripts.benchmark_research.subprocess.run", side_effect=child),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(execute(args, suite, plan), 2)
        run = example_run()
        run["metrics"]["search_stop_reason"] = "search_error"
        self.assertFalse(operational_checks(run, task())["passed"])

    def test_score_pending_is_nonzero_and_run_source_mismatch_is_rejected(self):
        from as_researcher.benchmark import source_digest
        from as_researcher.review import fingerprint

        suite_path = ROOT / "eval/research_suite.json"
        suite = load_suite(suite_path)
        case = suite.tasks[0]
        identity = "mcp_stdio__1__segments"
        run = example_run()
        run.update(
            benchmark={"trial_id": identity, "suite_sha256": digest(suite_path.read_bytes())},
            provenance={"source_sha256": source_digest()},
            excerpt_strategy="segments",
        )
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as directory:
            batch = Path(directory)
            folder = batch / identity / "record"
            folder.mkdir(parents=True)
            (batch / "suite.json").write_bytes(suite_path.read_bytes())
            trial = {
                "id": identity,
                "task_id": "mcp_stdio",
                "repeat": 1,
                "strategy": "segments",
                "status": "saved",
                "run_file": f"{identity}/record",
                "run_sha256": fingerprint(run),
            }
            plan = {
                "suite_sha256": digest(suite_path.read_bytes()),
                "source_sha256": source_digest(),
                "mode": "frozen",
                "trials": [trial],
            }
            (batch / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (batch / "index.json").write_text(json.dumps([trial]), encoding="utf-8")
            (folder / "run.json").write_text(json.dumps(run), encoding="utf-8")
            (folder / "task-review.draft.json").write_text(
                json.dumps(prepare_task_review(run, case)), encoding="utf-8"
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(score(batch), 3)
            value = json.loads(next(batch.glob("assessment-*.json")).read_bytes())
            self.assertEqual(value["summary"]["strategies"]["segments"]["unassessed"], 1)
            run["excerpt_strategy"] = "bm25"
            (folder / "run.json").write_text(json.dumps(run), encoding="utf-8")
            trial["run_sha256"] = fingerprint(run)
            (batch / "index.json").write_text(json.dumps([trial]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "planned trial"):
                score(batch)

    def test_timeout_keeps_remaining_trials_in_index(self):
        import subprocess

        suite = load_suite(ROOT / "eval/research_suite.json")
        args = SimpleNamespace(
            case=["mcp_stdio"],
            strategy=None,
            mode="web",
            transport="local",
            corpus=None,
            operation="run",
            repeats=2,
            suite=ROOT / "eval/research_suite.json",
            chars_per_page=800,
            answer_chars=700,
            timeout=30,
            model_max_tokens=1800,
            model_thinking="provider-default",
            search_provider="duckduckgo",
        )
        plan = make_plan(args, suite)
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as directory:
            args.output = Path(directory) / "batch"
            with (
                patch(
                    "scripts.benchmark_research.subprocess.run",
                    side_effect=subprocess.TimeoutExpired("worker", 70),
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(execute(args, suite, plan), 2)
            rows = json.loads((args.output / "index.json").read_bytes())
            self.assertEqual(
                [r["status"] for r in rows],
                ["process_deadline", "process_deadline", "not_run", "not_run"],
            )

    def test_default_plan_never_loads_env_constructs_model_or_writes(self):
        stdout = io.StringIO()
        with (
            patch("dotenv.load_dotenv") as dotenv,
            patch("as_researcher.models.live_model") as model,
            patch("pathlib.Path.mkdir") as mkdir,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["plan", "--case", "mcp_stdio"]), 0)
        dotenv.assert_not_called()
        model.assert_not_called()
        mkdir.assert_not_called()
        plan = json.loads(stdout.getvalue())
        self.assertEqual(plan["execution"], "plan_only")
        self.assertEqual(
            [t["strategy"] for t in plan["trials"]], ["segments", "bm25", "bm25", "segments"]
        )

    def test_unknown_selection_and_excess_trials_fail_before_subprocess(self):
        for arguments in (
            ["plan", "--case", "typo"],
            ["plan", "--case", "mcp_stdio", "--case", "mcp_stdio"],
            ["plan", "--timeout", "0"],
            ["plan", "--mode", "web", "--case", "injection"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch("scripts.benchmark_research.subprocess.run") as process,
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    main(arguments)
                process.assert_not_called()

    def test_two_failed_children_keep_remaining_planned_rows(self):
        suite = load_suite(ROOT / "eval/research_suite.json")
        args = SimpleNamespace(
            case=["mcp_stdio"],
            strategy=None,
            mode="web",
            transport="local",
            corpus=None,
            operation="run",
            repeats=2,
            suite=ROOT / "eval/research_suite.json",
            chars_per_page=800,
            answer_chars=700,
            timeout=30,
            model_max_tokens=1800,
            model_thinking="provider-default",
            search_provider="duckduckgo",
        )
        plan = make_plan(args, suite)
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as directory:
            args.output = Path(directory) / "batch"
            with (
                patch(
                    "scripts.benchmark_research.subprocess.run",
                    return_value=SimpleNamespace(returncode=2),
                ) as process,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(execute(args, suite, plan), 2)
            rows = json.loads((args.output / "index.json").read_bytes())
            self.assertEqual(
                [r["status"] for r in rows],
                ["worker_failed", "worker_failed", "not_run", "not_run"],
            )
            self.assertEqual(process.call_count, 2)
            self.assertFalse(process.call_args.kwargs.get("shell", False))
            self.assertEqual(
                process.call_args.kwargs["stdout"], -3
            )  # DEVNULL, no inherited log pipes

    def test_byte_and_url_mismatches_never_load_a_snapshot(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "reports") as directory:
            folder = Path(directory)
            raw = b"<p>Known evidence.</p>"
            (folder / "a.html").write_bytes(raw)
            (folder / "manifest.json").write_text(
                json.dumps(
                    {"pages": {"a": {"url": "https://example.org/a", "sha256": digest(raw)}}}
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                load_corpus(folder, {"a": "https://example.org/a"})["https://example.org/a"],
                raw.decode(),
            )
            for mapping in ({"a": "https://example.org/b"}, {"../a": "https://example.org/a"}):
                with self.assertRaises(ValueError):
                    load_corpus(folder, mapping)
            (folder / "a.html").write_bytes(raw + b" changed")
            with self.assertRaises(ValueError):
                load_corpus(folder, {"a": "https://example.org/a"})

    def test_suite_rejects_duplicate_tasks_and_unknown_documents(self):
        data = load_suite(ROOT / "eval/research_suite.json").model_dump()
        self.assertEqual(len(data["tasks"]), 20)
        self.assertEqual(sum(len(t["rubric"]) for t in data["tasks"]), 62)
        data["tasks"].append(copy.deepcopy(data["tasks"][0]))
        with self.assertRaises(ValueError):
            ResearchSuite.model_validate(data)
        data["tasks"].pop()
        data["tasks"][0]["documents"] = ["missing"]
        with self.assertRaises(ValueError):
            ResearchSuite.model_validate(data)
