"""Exercise generated questions and the actual fixture executor without paid calls."""

import argparse
import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from as_researcher.models import ScriptedModel
from as_researcher.researcher import SYSTEM_PROMPT, TOOL_OBSERVATION_VERSION, run_research
from scripts.evaluate_v08 import execute, main, omission_inputs

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "eval/omission_v014.json").read_text("utf-8"))


class ExperimentBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_one_budget_changes_question_and_config_together(self):
        for budget in (250, 320):
            fixture = copy.deepcopy(FIXTURE)
            fixture["max_answer_chars"] = budget
            topic, config = omission_inputs(fixture)
            self.assertIn(f"不超过{budget}个非空白字符", topic)
            self.assertEqual(config.max_answer_chars, budget)

    def test_legacy_and_ambiguous_templates_rejected(self):
        legacy = json.loads((ROOT / "eval/omission_v08.json").read_text("utf-8"))
        with self.assertRaises(ValueError):
            omission_inputs(legacy)
        for updates in (
            {"max_answer_chars": True},
            {"max_answer_chars": 99},
            {"topic": "700字"},
            {"topic_template": "250字以内"},
            {"topic_template": "{max_answer_chars}{max_answer_chars}"},
            {"topic_template": "{max_answer_chars:.0f}"},
            {"topic_template": "{unknown}"},
            {"cases": [{"id": "../escape", "chars_per_page": 500}]},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                omission_inputs(FIXTURE | updates)

    def test_selection_and_frozen_mismatches_fail_before_env_or_model(self):
        # The saved plan is historical. Start from today's actual fingerprints
        # so an unrelated source mismatch cannot make every negative case pass.
        baseline = FIXTURE | {
            "expected_source_sha256": hashlib.sha256(
                b"".join(
                    p.name.encode() + b"\0" + p.read_bytes()
                    for p in sorted((ROOT / "src/as_researcher").glob("*.py"))
                )
            ).hexdigest(),
            "expected_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "expected_executor_sha256": hashlib.sha256(
                (ROOT / "scripts/evaluate_v08.py").read_bytes()
            ).hexdigest(),
            "expected_tool_observation_version": TOOL_OBSERVATION_VERSION,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            for changes, args in (
                ({}, ["--case", "unknown"]),
                ({}, ["--case", "retained_example_other_omitted"] * 2),
                ({"expected_source_sha256": "wrong"}, []),
                ({"expected_prompt_sha256": "wrong"}, []),
                ({"expected_executor_sha256": "wrong"}, []),
                ({"expected_tool_observation_version": 999}, []),
                ({"model_max_tokens": 700}, []),
            ):
                path.write_text(json.dumps(baseline | changes), "utf-8")
                error = io.StringIO()
                with (
                    patch("scripts.evaluate_v08.live_model") as model,
                    patch("scripts.evaluate_v08.load_dotenv") as env,
                    contextlib.redirect_stderr(error),
                ):
                    with self.assertRaises(SystemExit) as exc:
                        main(["--suite", "omission", "--manifest", str(path), "--execute", *args])
                    self.assertEqual(exc.exception.code, 2)
                    self.assertIn(
                        next(iter(changes)) if changes else "Unknown or duplicate selected case",
                        error.getvalue(),
                    )
                    model.assert_not_called()
                    env.assert_not_called()

    async def test_executor_passes_same_budget_to_real_agent_and_saved_run(self):
        fixture = copy.deepcopy(FIXTURE)
        fixture.pop("expected_model", None)
        topic, config = omission_inputs(fixture)
        plan = {
            "manifest": fixture,
            "manifest_sha256": "test-fixture-only",
            "resolved_topic": topic,
            "research_config": config.model_dump(),
            "selected_cases": [fixture["cases"][1]["id"]],
        }
        model = ScriptedModel("tool_chain")
        model.client = SimpleNamespace(close=AsyncMock())
        args = argparse.Namespace(suite="omission", model_max_tokens=1200)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("scripts.evaluate_v08.ROOT", root),
                patch("scripts.evaluate_v08.live_model", return_value=model) as factory,
                patch("scripts.evaluate_v08.run_research", wraps=run_research) as research,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                await execute(args, plan)
            factory.assert_called_once()
            research.assert_awaited_once()
            self.assertEqual(research.call_args.args[0], topic)
            self.assertEqual(research.call_args.args[3].max_answer_chars, 250)
            self.assertEqual(research.call_args.args[3].chars_per_page, 180)
            files = list(root.rglob("run.json"))
            self.assertEqual(len(files), 1)
            run = json.loads(files[0].read_text("utf-8"))
            self.assertEqual(run["topic"], topic)
            self.assertEqual(run["config"]["max_answer_chars"], 250)
            self.assertEqual(
                run["metrics"]["answer_within_limit"], run["metrics"]["answer_chars"] <= 250
            )
            self.assertEqual(
                (run["metrics"]["search_attempts"], run["metrics"]["page_read_attempts"]), (1, 1)
            )
            model.client.close.assert_awaited_once()

    def test_historical_plan_can_be_inspected_but_cannot_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "historical.json"
            path.write_text(json.dumps(FIXTURE | {"expected_source_sha256": "old-source"}), "utf-8")
            raw = path.read_bytes()
            output = io.StringIO()
            with (
                patch("scripts.evaluate_v08.live_model") as model,
                patch("scripts.evaluate_v08.load_dotenv") as env,
                patch("pathlib.Path.mkdir") as mkdir,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                args = [
                    "--suite",
                    "omission",
                    "--manifest",
                    str(path),
                    "--case",
                    "requested_example_omitted",
                ]
                self.assertEqual(main(args), 0)
                shown = json.loads(output.getvalue()[output.getvalue().index("{") :])
                self.assertIn("expected_source_sha256", shown["execution_blockers"])
                self.assertEqual(shown["selected_cases"], ["requested_example_omitted"])
                with self.assertRaises(SystemExit) as exc:
                    main(args + ["--execute"])
                self.assertEqual(exc.exception.code, 2)
                model.assert_not_called()
                env.assert_not_called()
                mkdir.assert_not_called()
            self.assertEqual(path.read_bytes(), raw)
