import asyncio
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts.replay_revision import execute, load_plan, main
from scripts.review_revision import main as review_main
from tests.test_comparison import example


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "reports/old/run.json"
        self.path.parent.mkdir(parents=True)
        self.run, self.revision = example()
        self.run["answer"] *= 6
        self.run.update(
            schema_version=6,
            topic="fixture",
            mode="offline_fixture",
            metrics={"status": "completed", "stop_reason": "agent_completed"},
        )
        self.path.write_text(json.dumps(self.run, ensure_ascii=False), "utf-8")
        self.plan = {
            "schema_version": 1,
            "mode": "saved_evidence_revision_replay",
            "max_answer_chars": 100,
            "revision_max_tokens": 1200,
            "revision_timeout": 30,
            "cases": [
                {
                    "id": "example",
                    "run_file": "reports/old/run.json",
                    "run_file_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                    "original_answer_sha256": hashlib.sha256(
                        self.run["answer"].encode()
                    ).hexdigest(),
                    "review_checklist": ["Keep version and negation"],
                }
            ],
        }
        self.plan_path = self.root / "plan.json"

    def load(self, ids=None):
        self.plan_path.write_text(json.dumps(self.plan), "utf-8")
        return load_plan(self.plan_path, ids, self.root)

    def test_plan_rejects_changed_evidence_before_any_paid_call(self):
        self.load()
        self.path.write_text(self.path.read_text("utf-8") + " ", "utf-8")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.load()

    def test_frozen_prompt_and_explicit_thinking_mode_preflight(self):
        self.plan["revision_thinking"] = "disabled"
        _, config, _ = self.load()
        self.assertEqual(config.revision_thinking, "disabled")
        self.plan["revision_thinking"] = "unknown"
        with self.assertRaises(ValueError):
            self.load()
        self.plan["revision_thinking"] = "disabled"
        self.plan["expected_prompt_sha256"] = "wrong"
        with self.assertRaisesRegex(ValueError, "prompt fingerprint"):
            self.load()
        self.plan.pop("expected_prompt_sha256")
        self.plan["cases"][0]["expected_input_sha256"] = "wrong"
        with self.assertRaisesRegex(ValueError, "input fingerprint"):
            self.load()

    def test_budget_selection_path_and_completed_status_checked(self):
        for key, value in (("max_answer_chars", 99), ("revision_timeout", 61)):
            before = self.plan[key]
            self.plan[key] = value
            with self.assertRaises(ValueError):
                self.load()
            self.plan[key] = before
        for ids in (["wrong"], ["example", "example"]):
            with self.assertRaises(ValueError):
                self.load(ids)
        self.plan["cases"][0]["run_file"] = "../.env"
        with self.assertRaises(ValueError):
            self.load()

    def test_default_plan_has_no_secret_loading_model_or_directory_side_effects(self):
        with (
            patch("scripts.replay_revision.live_model") as model,
            patch("scripts.replay_revision.load_dotenv") as env,
            patch("pathlib.Path.mkdir") as mkdir,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["--case", "shield_saved_v07"]), 0)
            model.assert_not_called()
            env.assert_not_called()
            mkdir.assert_not_called()

    def test_execution_freezes_original_and_produces_independent_readonly_comparison(self):
        from as_researcher.revision import answer_chars, digest

        raw, config, loaded = self.load()
        self.revision.update(
            original_answer=self.run["answer"],
            original_chars=answer_chars(self.run["answer"]),
            original_sha256=digest(self.run["answer"]),
        )
        initial = self.path.read_bytes()
        model = type("Model", (), {"client": type("Client", (), {"close": AsyncMock()})()})()
        with (
            patch("scripts.replay_revision.live_model", return_value=model),
            patch(
                "scripts.replay_revision.revise_answer",
                new_callable=AsyncMock,
                return_value=self.revision,
            ) as call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(asyncio.run(execute(raw, config, loaded, self.root / "out")), 3)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(self.path.read_bytes(), initial)
        folder = next((self.root / "out").glob("*/example"))
        self.assertEqual((folder / "source-run.json").read_bytes(), initial)
        packet = json.loads((folder / "comparison.json").read_text("utf-8"))
        self.assertEqual(packet["reviewer"]["kind"], "unassigned")
        output = folder / "export.html"
        args = [
            "export",
            str(folder / "source-run.json"),
            str(folder / "revision.json"),
            str(folder / "comparison.json"),
            "--output",
            str(output),
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review_main(args), 0)
        with self.assertRaises(FileExistsError):
            review_main(args)

    def test_incomplete_response_stops_remaining_replay_without_retry(self):
        from as_researcher.revision import answer_chars, digest

        raw, config, loaded = self.load()
        self.revision.update(
            original_answer=self.run["answer"],
            original_chars=answer_chars(self.run["answer"]),
            original_sha256=digest(self.run["answer"]),
            candidate_answer=None,
            status="rejected",
            applied=False,
            errors=["revision_completion_unverified"],
        )
        model = type("Model", (), {"client": type("Client", (), {"close": AsyncMock()})()})()
        with (
            patch("scripts.replay_revision.live_model", return_value=model),
            patch(
                "scripts.replay_revision.revise_answer",
                new_callable=AsyncMock,
                return_value=self.revision,
            ) as call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(asyncio.run(execute(raw, config, loaded * 2, self.root / "out")), 2)
        self.assertEqual(call.await_count, 1)

    def test_model_drift_rejected_before_paid_call_and_client_closed(self):
        self.plan["expected_model"] = "deepseek-flash"
        raw, config, loaded = self.load()
        close = AsyncMock()
        model = type(
            "Model", (), {"model": "different", "client": type("Client", (), {"close": close})()}
        )()
        with (
            patch("scripts.replay_revision.live_model", return_value=model),
            patch("scripts.replay_revision.revise_answer", new_callable=AsyncMock) as call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(asyncio.run(execute(raw, config, loaded, self.root / "out")), 2)
        call.assert_not_awaited()
        close.assert_awaited_once()
        metadata = json.loads(
            next((self.root / "out").glob("*/example/replay.json")).read_text("utf-8")
        )
        self.assertEqual(metadata["status"], "setup_error")
