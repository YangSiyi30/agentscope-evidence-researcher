"""Historical provenance and independent review units, without semantic scoring."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import inspect_edit_contract as contract


def temporary_fixture(root):
    fixture = json.loads(contract.DEFAULT_FIXTURE.read_bytes())
    base = json.loads(contract.check.DEFAULT_FIXTURE.read_bytes())
    paths = [fixture["base_fixture_file"]]
    paths.extend(item["file"] for item in fixture["source_binding"].values())
    paths.extend(
        base["source_binding"][key]
        for key in (
            "revision_file",
            "answer_file",
            "source_run_file",
            "historical_run_file",
        )
    )
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((contract.ROOT / relative).read_bytes())
    path = root / "fixture.json"
    path.write_text(json.dumps(fixture), "utf-8")
    return path, fixture


class EditContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = contract.load_fixture(contract.DEFAULT_FIXTURE)
        cls.result = contract.inspect(cls.loaded)
        cls.rows = {row["id"]: row for row in cls.result["rows"]}
        cls.fields = cls.rows["qualified_fields"]["draft_payload"]

    def test_default_is_read_only_and_does_not_load_paid_plan(self):
        stream = io.StringIO()
        with (
            patch("dotenv.load_dotenv", side_effect=AssertionError("no environment load")),
            patch("socket.create_connection", side_effect=AssertionError("no network")),
            patch("as_researcher.models.live_model", side_effect=AssertionError("no model")),
            patch(
                "scripts.execute_minimal_edit.load_plan",
                side_effect=AssertionError("no old execution"),
            ),
            patch.object(Path, "mkdir", side_effect=AssertionError("no directory creation")),
            patch.object(Path, "write_text", side_effect=AssertionError("no writes")),
            contextlib.redirect_stdout(stream),
        ):
            self.assertEqual(contract.main([]), 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["model_calls"], result["searches"], 0)
        self.assertEqual(result["page_reads"], 0)
        self.assertTrue(any(not row["mechanically_valid"] for row in result["rows"]))
        # Exit 0 means inspection completed; intentionally invalid drafts remain.

    def test_four_actual_artifacts_reject_changed_raw_bytes(self):
        for role in self.loaded["fixture"]["source_binding"]:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path, fixture = temporary_fixture(root)
                target = root / fixture["source_binding"][role]["file"]
                target.write_bytes(target.read_bytes() + b"\n")
                with self.assertRaisesRegex(ValueError, "contract_artifact_hash_mismatch"):
                    contract.load_fixture(path, root)

    def test_rebound_changed_response_cannot_diverge_from_saved_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, fixture = temporary_fixture(root)
            item = fixture["source_binding"]["response"]
            target = root / item["file"]
            target.write_bytes(target.read_bytes() + b" ")
            item["sha256"] = contract.check.sha(target.read_bytes())
            path.write_text(json.dumps(fixture), "utf-8")
            with self.assertRaisesRegex(ValueError, "saved_response_mismatch"):
                contract.load_fixture(path, root)

    def test_saved_candidate_request_plan_and_review_cross_checks(self):
        changes = (
            ("edit", lambda d: d["candidate_check"].update(answer_chars=606)),
            ("edit", lambda d: d["request"].update(user="changed")),
            ("plan", lambda d: d.update(expected_model="other")),
            ("assistant_review", lambda d: d.update(human_gold=True)),
            ("assistant_review", lambda d: d.update(fixture_sha256="0" * 64)),
        )
        for role, change in changes:
            with self.subTest(role=role, change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path, fixture = temporary_fixture(root)
                item = fixture["source_binding"][role]
                target = root / item["file"]
                data = json.loads(target.read_bytes())
                change(data)
                target.write_text(json.dumps(data), "utf-8")
                item["sha256"] = contract.check.sha(target.read_bytes())
                path.write_text(json.dumps(fixture), "utf-8")
                with self.assertRaises(ValueError):
                    contract.load_fixture(path, root)

    def test_paths_base_hash_and_draft_identity_are_checked(self):
        mutations = (
            lambda f: f["source_binding"]["response"].update(file="../outside.txt"),
            lambda f: f["source_binding"]["edit"].update(file="reports/secret.env"),
            lambda f: f.update(base_fixture_sha256="0" * 64),
            lambda f: f["drafts"][0].update(id="saved_model_response"),
            lambda f: f["drafts"][0].update(format="other"),
            lambda f: f["drafts"][0].update(semantic_score=1),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path, fixture = temporary_fixture(root)
                mutation(fixture)
                path.write_text(json.dumps(fixture), "utf-8")
                with self.assertRaises(ValueError):
                    contract.load_fixture(path, root)

    def test_free_and_fields_are_same_content_with_different_json_overhead(self):
        free, fields = self.rows["qualified_free"], self.rows["qualified_fields"]
        self.assertEqual(free["format_check"]["replacement"], fields["format_check"]["replacement"])
        self.assertEqual(free["candidate_check"]["replacement_chars"], 133)
        self.assertEqual(fields["candidate_check"]["answer_chars"], 695)
        self.assertTrue(free["mechanically_valid"] and fields["mechanically_valid"])
        self.assertGreater(fields["draft_json_characters"], free["draft_json_characters"])
        self.assertIsNone(self.result["semantic_score"])

    def test_complete_fields_can_be_contradictory_or_add_unsupported_versions(self):
        for cid in ("contradictory_fields", "unsupported_version_fields"):
            row = self.rows[cid]
            self.assertTrue(row["format_check"]["valid"])
            self.assertTrue(row["mechanically_valid"])
            for unit in row["review_units"].values():
                self.assertIsNone(unit["semantic_review"]["assessment"])
                self.assertEqual(unit["semantic_review"]["reviewer"]["kind"], "unassigned")
        self.assertFalse(self.rows["wrong_level_fields"]["mechanically_valid"])
        self.assertIn(
            "citation_refs_and_levels_preserved",
            self.rows["wrong_level_fields"]["candidate_check"]["errors"],
        )

    def test_missing_extra_empty_and_nonstring_fields_are_not_rendered(self):
        for payload in (
            self.fields | {"unknown": "text"},
            {k: v for k, v in self.fields.items() if k != "cancellation_trigger"},
            self.fields | {"scope": " \n"},
            self.fields | {"scope": False},
            self.fields | {"scope": ["text"]},
            "not an object",
        ):
            shape = contract.format_draft("structured_fields", payload)
            self.assertFalse(shape["valid"])
            self.assertIsNone(shape["replacement"])
        self.assertFalse(contract.format_draft("free_text", " \n")["valid"])
        self.assertFalse(contract.format_draft("other", self.fields)["valid"])
        for cid in ("missing_trigger_field", "extra_field"):
            self.assertIsNone(self.rows[cid]["candidate_check"])
            self.assertIsNone(self.rows[cid]["review_units"]["reconstructed_answer"]["text"])

    def test_field_values_are_preserved_but_do_not_expand_edit_budget(self):
        payload = self.fields | {"scope": " 仅核验片段\n"}
        rendered = contract.format_draft("structured_fields", payload)["replacement"]
        self.assertIn("； 仅核验片段\n；", rendered)
        loaded = copy.deepcopy(self.loaded)
        loaded["fixture"]["drafts"] = [
            {
                "id": "over",
                "format": "structured_fields",
                "payload": self.fields | {"scope": "长" * 200},
            }
        ]
        row = contract.inspect(loaded)["rows"][1]
        self.assertTrue(row["format_check"]["valid"])
        self.assertFalse(row["mechanically_valid"])
        self.assertIn("replacement_within_budget", row["candidate_check"]["errors"])

    def test_actual_short_replacement_and_preserved_context_are_separate_units(self):
        row = self.rows["saved_model_response"]
        self.assertEqual(row["origin"], "saved_model_adapter_text")
        self.assertEqual(row["candidate_check"]["replacement_chars"], 43)
        self.assertEqual(row["candidate_check"]["answer_chars"], 605)
        units = row["review_units"]
        self.assertNotIn("CancelledError", units["replacement_only"]["text"])
        self.assertIn("CancelledError", units["reconstructed_answer"]["text"])
        original = self.loaded["context"]["answer"]
        target = self.loaded["context"]["fixture"]["edit_task"]["target"]
        self.assertEqual(
            units["reconstructed_answer"]["text"],
            original[: target["start"]]
            + units["replacement_only"]["text"]
            + original[target["end"] :],
        )
        self.assertEqual(
            self.result["prior_model_record"]["token_usage"], {"input": 1252, "output": 26}
        )
        self.assertTrue(
            all(row["origin"] == "assistant_handwritten_draft" for row in self.result["rows"][1:])
        )

    def test_semantic_review_objects_are_independent_and_no_gold_is_assigned(self):
        result = contract.inspect(self.loaded)
        units = result["rows"][0]["review_units"]
        units["replacement_only"]["semantic_review"]["assessment"] = "manual placeholder"
        self.assertIsNone(units["reconstructed_answer"]["semantic_review"]["assessment"])
        self.assertIsNone(
            result["rows"][1]["review_units"]["replacement_only"]["semantic_review"]["assessment"]
        )
        self.assertFalse(result["human_review_complete"])

    def test_html_escapes_draft_text_and_explicit_export_refuses_overwrite(self):
        loaded = copy.deepcopy(self.loaded)
        loaded["fixture"]["drafts"] = [
            {
                "id": "<script>x</script>",
                "format": "structured_fields",
                "payload": self.fields | {"scope": "<script>alert(1)</script>"},
            }
        ]
        rendered = contract.render_inspection(contract.inspect(loaded))
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("default-src 'none'", rendered)
        self.assertIn("replacement_only", rendered)
        self.assertIn("reconstructed_answer", rendered)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "inspection.html"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(contract.main(["--output", str(output)]), 0)
            original = output.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                contract.main(["--output", str(output)])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
