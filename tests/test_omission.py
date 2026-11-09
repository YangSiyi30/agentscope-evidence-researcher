import json
import unittest
from pathlib import Path

from as_researcher.core import ResearchConfig, Source
from as_researcher.evidence import page_observation
from as_researcher.researcher import ResearchSession
from as_researcher.segments import parse_document, select_passages

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "eval/omission_v08.json").read_text("utf-8"))


class OmissionTests(unittest.TestCase):
    def test_complementary_frozen_cases_identify_the_actual_omitted_group(self):
        doc = parse_document(FIXTURE["html"])
        target = next(p for p in doc["passages"] if p["kind"] == "code_context")
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["id"]):
                result = select_passages(doc, FIXTURE["focus"], case["chars_per_page"])
                self.assertLessEqual(len(result["content"]), case["chars_per_page"])
                state = result["code_selection"]
                retained = {p["segment_id"] for p in state["retained"]["details"]}
                omitted = {p["segment_id"] for p in state["omitted"]["details"]}
                self.assertFalse(retained & omitted)
                if case["chars_per_page"] == 500:
                    self.assertIn(target["segment_id"], retained)
                    self.assertIn("result = await work()", result["content"])
                    self.assertEqual(state["omitted"]["total"], 1)
                else:
                    self.assertIn(target["segment_id"], omitted)
                    self.assertNotIn("result = await work()", result["content"])
                    self.assertEqual(state["retained"]["total"], 0)
                self.assertEqual(state["omitted"]["reason"], "page_character_budget")

    def test_detail_lists_and_heading_sizes_bound_extra_context(self):
        doc = parse_document("<h1>" + "x" * 300 + "</h1>" + "<pre>code()</pre>" * 50)
        result = select_passages(doc, "code", 1200)
        state = result["code_selection"]
        for key in ("retained", "omitted"):
            group = state[key]
            self.assertLessEqual(len(group["details"]), 4)
            self.assertEqual(group["total"], len(group["details"]) + group["unlisted"])
            self.assertTrue(all(len(d["heading"]) <= 80 for d in group["details"]))
        self.assertLess(len(json.dumps(state, ensure_ascii=False)), 2500)
        self.assertIn("not_whole_definition", state["completeness_basis"])

    def test_model_observation_removes_ambiguous_counts_without_mutating_audit(self):
        page = select_passages(parse_document(FIXTURE["html"]), FIXTURE["focus"], 500)
        before = json.dumps(page, sort_keys=True)
        observation = page_observation(page)
        self.assertNotIn("segments", observation)
        self.assertNotIn("omitted_code_blocks", observation)
        self.assertNotIn("code_blocks_omitted_for_budget", observation["selection_warnings"])
        self.assertEqual(page["code_selection"], observation["code_selection"])
        self.assertEqual(before, json.dumps(page, sort_keys=True))
        old_page = {"content": "old", "omitted_code_blocks": 1}
        self.assertEqual(page_observation(old_page), old_page)


class ObservationContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversize_reader_cannot_label_clipped_code_as_complete(self):
        source = Source("fixture", "https://example.org/code", "snippet")

        async def search(query, limit):
            return [source]

        async def reader(url, limit, focus=""):
            return select_passages(parse_document(FIXTURE["html"]), FIXTURE["focus"], 500)

        session = ResearchSession(ResearchConfig(chars_per_page=180), search, reader)
        await session.search_web("test")
        response = await session.read_page(source.id)
        self.assertEqual(json.loads(response.content[0].text)["error"], "page_budget_exceeded")
        self.assertEqual(session.page_attempts, 1)
        self.assertEqual(session.page_evidence, {})
