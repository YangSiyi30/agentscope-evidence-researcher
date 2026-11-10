"""A clone can inspect genuine bad/good historical answers without paid reruns."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from as_researcher import evaluation_terminal as ui
from as_researcher.benchmark import assess_saved_batch, digest

ROOT = Path(__file__).resolve().parents[1]


class PortfolioTests(unittest.TestCase):
    def test_portable_batch_hashes_and_original_outcomes_are_preserved(self):
        base = ROOT / "examples/portfolio"
        manifest = json.loads((base / "manifest.json").read_bytes())
        self.assertTrue(
            all(
                digest((base / "batch" / p).read_bytes()) == h for p, h in manifest["files"].items()
            )
        )
        report = assess_saved_batch(base / "batch")
        self.assertEqual(len(report["rows"]), 16)
        self.assertEqual(report["summary"]["strategies"]["segments__standard"]["passed"], 3)
        self.assertEqual(report["summary"]["strategies"]["segments__compact"]["passed"], 5)
        self.assertTrue(
            all(r["assessment"]["quality"]["reviewer_kind"] == "assistant" for r in report["rows"])
        )

    def test_readonly_pages_have_no_review_or_mutation_actions(self):
        batch = ROOT / "examples/portfolio/batch"
        context = assess_saved_batch(batch)
        menus = []

        def choose(title, options, default):
            menus.append(options)
            return "0"

        with (
            patch.object(ui, "choice", side_effect=choose),
            patch.object(ui, "review_trial") as review,
            patch.object(ui, "run_operation") as operation,
            redirect_stdout(io.StringIO()),
        ):
            ui.view_batch(batch, readonly=True)
            ui.view_trial(context, context["rows"][0], batch, readonly=True)
        self.assertTrue({"s", "u", "o"}.isdisjoint(menus[0]))
        self.assertNotIn("4", menus[1])
        review.assert_not_called()
        operation.assert_not_called()

    def test_example_entry_does_not_construct_model_or_load_dotenv(self):
        with (
            patch("builtins.input", side_effect=["7", "0"]),
            patch.object(ui, "view_batch") as view,
            patch("dotenv.load_dotenv", side_effect=AssertionError("no env")),
            patch("as_researcher.models.live_model", side_effect=AssertionError("no model")),
            redirect_stdout(io.StringIO()),
        ):
            ui.run_evaluation_center()
        view.assert_called_once_with(ROOT / "examples/portfolio/batch", readonly=True)

    def test_relocated_corpus_must_match_hash_and_stay_in_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            corpus.mkdir()
            raw = b'{"pages":{}}'
            (corpus / "manifest.json").write_bytes(raw)
            plan = {"corpus": "C:/old/now-missing", "corpus_manifest_sha256": digest(raw)}
            with patch.object(ui, "ROOT", root), patch.object(ui, "CORPUS", corpus):
                self.assertEqual(ui.available_corpus(plan), corpus.resolve())
                plan["corpus_manifest_sha256"] = "wrong"
                self.assertIsNone(ui.available_corpus(plan))


if __name__ == "__main__":
    unittest.main()
