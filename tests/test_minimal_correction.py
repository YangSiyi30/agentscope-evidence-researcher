"""Saved-edit integrity, immutable boundaries and the gap between structure and meaning."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_minimal_correction as check


def temporary_fixture(root, mutation=None):
    fixture = json.loads(check.DEFAULT_FIXTURE.read_bytes())
    for key in ("revision_file", "answer_file", "source_run_file", "historical_run_file"):
        relative = fixture["source_binding"][key]
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((check.ROOT / relative).read_bytes())
    if mutation:
        mutation(fixture)
    path = root / "fixture.json"
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    return path, fixture


class MinimalCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = check.load_context(check.DEFAULT_FIXTURE)
        cls.good = cls.context["fixture"]["handwritten_candidates"][0]

    def test_saved_fixture_and_semantic_counterexamples_stay_distinct(self):
        result = check.inspect(self.context)
        rows = {row["id"]: row for row in result["candidates"]}
        self.assertEqual([row["answer_chars"] for row in rows.values()], [698, 608, 656, 674, 661])
        for cid in (
            "correct_with_cancellation_qualification",
            "copy_original_error",
            "drop_cancellation_qualification",
            "add_unverified_version_range",
        ):
            self.assertTrue(rows[cid]["mechanically_valid"])
        self.assertEqual(
            rows["move_body_claim_to_snippet"]["errors"], ["citation_refs_and_levels_preserved"]
        )
        self.assertIsNone(result["semantic_score"])
        self.assertFalse(result["human_review_complete"])
        for row in rows.values():
            self.assertIsNone(row["semantic_review"]["assessment"])
            self.assertEqual(row["semantic_review"]["reviewer"]["kind"], "unassigned")

    def test_default_cli_only_reads_without_dotenv_network_or_writes(self):
        stream = io.StringIO()
        with (
            patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv must not load")),
            patch("socket.create_connection", side_effect=AssertionError("must stay offline")),
            patch.object(Path, "mkdir", side_effect=AssertionError("must not create output")),
            patch.object(Path, "write_text", side_effect=AssertionError("must not write")),
            contextlib.redirect_stdout(stream),
        ):
            self.assertEqual(check.main([]), 1)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["model_calls"], result["searches"], 0)
        self.assertEqual(result["page_reads"], 0)
        for relative, expected in result["bound_files_sha256"].items():
            self.assertEqual(check.sha((check.ROOT / relative).read_bytes()), expected)

    def test_all_bound_file_fingerprints_and_escape_paths_are_rejected(self):
        for key in ("revision_file", "answer_file", "source_run_file", "historical_run_file"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path, fixture = temporary_fixture(root)
                source = root / fixture["source_binding"][key]
                source.write_bytes(source.read_bytes() + b" ")
                with self.assertRaisesRegex(ValueError, key + "_fingerprint_mismatch"):
                    check.load_context(path, root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _ = temporary_fixture(
                root, lambda f: f["source_binding"].update(answer_file="../outside.md")
            )
            with self.assertRaisesRegex(ValueError, "invalid_bound_path"):
                check.load_context(path, root)

    def test_wrong_offsets_fixed_quote_scope_and_duplicate_ids_are_rejected(self):
        mutations = (
            lambda f: f["edit_task"]["target"].update(start=True),
            lambda f: f["edit_task"]["target"].update(end=620),
            lambda f: f["fixed_evidence"]["page_quote"].update(text="different saved evidence"),
            lambda f: f["fixed_evidence"]["scope_metadata"].update(omitted_code_blocks=4),
            lambda f: f["handwritten_candidates"].append(
                copy.deepcopy(f["handwritten_candidates"][0])
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path, _ = temporary_fixture(root, mutation)
                with self.assertRaises(ValueError):
                    check.load_context(path, root)

    def test_markdown_raw_bytes_and_crlf_text_are_checked_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, fixture = temporary_fixture(root)
            answer_path = root / fixture["source_binding"]["answer_file"]
            content = answer_path.read_bytes()
            self.assertIn(b"\r\n", content)
            self.assertNotEqual(check.sha(content), fixture["source_binding"]["answer_text_sha256"])
            self.assertEqual(check.load_context(path, root)["answer"], self.context["answer"])
            # Even a newly declared byte hash cannot hide extra answer text.
            content += b"\n"
            answer_path.write_bytes(content)
            fixture["source_binding"]["answer_file_sha256"] = check.sha(content)
            path.write_text(json.dumps(fixture), "utf-8")
            with self.assertRaisesRegex(ValueError, "answer_markdown_text_mismatch"):
                check.load_context(path, root)

    def test_empty_over_budget_unknown_or_unclosed_references_fail(self):
        for replacement, error in (
            (" \n", "replacement_nonempty"),
            ("长" * 139 + " [SRC-86481391@page]", "replacement_within_budget"),
            (self.good["text"] + " [SRC-00000000@page]", "citation_refs_and_levels_preserved"),
            (self.good["text"] + " [SRC-unclosed", "citation_refs_and_levels_preserved"),
        ):
            row = check.check_edit(self.context, {"id": "external", "text": replacement})
            self.assertFalse(row["mechanically_valid"])
            self.assertIn(error, row["errors"])
        with self.assertRaisesRegex(ValueError, "invalid_replacement_text"):
            check.check_edit(self.context, {"id": "external", "text": None})

    def test_provided_answer_cannot_change_surroundings_or_hide_a_different_edit(self):
        canonical = check.check_edit(self.context, self.good)["reconstructed_answer"]
        for answer in ("改" + canonical, canonical[:-1] + "改"):
            row = check.check_edit(self.context, self.good | {"reconstructed_answer": answer})
            self.assertFalse(row["checks"]["outside_target_unchanged"])
            self.assertFalse(row["checks"]["answer_matches_exact_replacement"])
        old = self.context["answer"]
        row = check.check_edit(self.context, self.good | {"reconstructed_answer": old})
        self.assertTrue(row["checks"]["outside_target_unchanged"])
        self.assertFalse(row["checks"]["answer_matches_exact_replacement"])

    def test_development_expectations_never_drive_the_computed_result(self):
        context = copy.deepcopy(self.context)
        for candidate in context["fixture"]["handwritten_candidates"]:
            candidate["expected_development_assessment"] = (
                "fail" if candidate["expected_development_assessment"] == "pass" else "pass"
            )
        left, right = check.inspect(self.context), check.inspect(context)
        self.assertEqual(
            [row["checks"] for row in left["candidates"]],
            [row["checks"] for row in right["candidates"]],
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.main(["--candidate", "copy_original_error"]), 0)

    def test_external_export_escapes_text_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            candidate_file, output = folder / "candidate.json", folder / "review.html"
            candidate = {
                "schema_version": 1,
                "id": "external",
                "text": "<script>alert('x')</script> [SRC-86481391@page]",
            }
            candidate_file.write_text(json.dumps(candidate), "utf-8")
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(
                    check.main(["--candidate-file", str(candidate_file), "--output", str(output)]),
                    0,
                )
            result = json.loads(stream.getvalue())
            self.assertEqual(
                result["candidate_file_sha256"], check.sha(candidate_file.read_bytes())
            )
            self.assertEqual(result["candidates"][0]["origin"], "unassigned_offline_candidate_file")
            self.assertIsNone(result["candidates"][0]["development_expectation"]["assessment"])
            rendered = output.read_text("utf-8")
            self.assertNotIn("<script>", rendered)
            self.assertIn("&lt;script&gt;", rendered)
            self.assertIn("default-src 'none'", rendered)
            original = output.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                check.main(["--output", str(output)])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(output.read_bytes(), original)

    def test_bad_selection_input_or_structural_failure_has_clear_exit_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            output = folder / "review.html"
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                check.main(["--candidate", "unknown", "--output", str(output)])
            self.assertFalse(output.exists())
            candidate_file = folder / "candidate.json"
            candidate_file.write_text(
                '{"schema_version": true, "id":"bad", "text":"secret error content"}', "utf-8"
            )
            stream = io.StringIO()
            with contextlib.redirect_stderr(stream), self.assertRaises(SystemExit):
                check.main(["--candidate-file", str(candidate_file), "--output", str(output)])
            self.assertNotIn("secret error content", stream.getvalue())
            self.assertFalse(output.exists())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    check.main(
                        ["--candidate", "move_body_claim_to_snippet", "--output", str(output)]
                    ),
                    1,
                )
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
