import copy
import json
import unittest
from pathlib import Path

from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse

from as_researcher.atomic_review import prepare_atomic_review
from as_researcher.core import ResearchConfig, Source, citation_diagnostics, source_id
from as_researcher.evidence import evidence_diagnostics
from as_researcher.models import ScriptedModel
from as_researcher.pages import PageReadError
from as_researcher.researcher import ResearchSession, render_report, run_research
from as_researcher.review import check_review, prepare_review

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "eval/evidence_levels_v06.json").read_text("utf-8")
)["cases"]
SID = "SRC-12345678"


class EvidenceLevelTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            "answer": f"保留 7 天。[{SID}@page]",
            "sources": [{"id": SID, "content": "保留 7 天。"}],
            "page_evidence": [{"id": SID, "content": "单次 4096 条。"}],
        }

    def test_same_url_preserves_one_source_identity_with_two_levels(self):
        answer = f"甲 [{SID}@snippet] 乙 [{SID}@page]"
        # Identity and evidence availability are independent diagnostics.
        source = Source("title", "https://example.org", "data")
        answer = answer.replace(SID, source.id)
        self.assertEqual(citation_diagnostics(answer, [source])["valid_ids"], [source.id])
        levels = evidence_diagnostics(
            answer, [{"id": source.id, "content": "甲"}], [{"id": source.id, "content": "乙"}]
        )
        self.assertTrue(levels["contract_valid"])
        self.assertEqual(len(levels["references"]), 2)

    def test_legacy_unknown_level_empty_page_and_invented_id_are_flagged(self):
        answer = f"[{SID}] [{SID}@body] [{SID}@page] [SRC-00000000@snippet]"
        levels = evidence_diagnostics(answer, self.run["sources"], [{"id": SID, "content": " "}])
        self.assertEqual(levels["untyped_references"], [SID])
        self.assertEqual(levels["invalid_references"], [SID + "@body"])
        self.assertEqual(levels["unavailable_references"], ["SRC-00000000@snippet", SID + "@page"])
        self.assertFalse(levels["contract_valid"])
        self.assertFalse(evidence_diagnostics("no facts", [], [])["contract_valid"])

    def test_availability_does_not_pretend_to_check_entailment(self):
        levels = evidence_diagnostics(
            self.run["answer"], self.run["sources"], self.run["page_evidence"]
        )
        self.assertTrue(levels["contract_valid"])
        # The page exists but does not support retention: review must catch this separately.
        for prepare in (prepare_atomic_review, prepare_review):
            packet = prepare(self.run)
            packet["reviewer"] = {"kind": "assistant", "name": "test fixture"}
            unit = packet["units"][0]
            claim = unit["claims"][0] if "claims" in unit else unit
            claim.update(
                label="supported",
                reason="Test deliberately uses wrong evidence level",
                evidence=[{"source_id": SID, "kind": "snippet", "quote": "保留 7 天"}],
            )
            result = check_review(self.run, packet)
            self.assertFalse(result["valid"])
            self.assertTrue(any("level_mismatch" in e for e in result["errors"]))
            claim["evidence"][0]["kind"] = "page"
            self.assertTrue(
                any(
                    "quote_not_in_saved_evidence" in e
                    for e in check_review(self.run, packet)["errors"]
                )
            )

    def test_atomic_reference_selection_cannot_change_kind_or_drop_contract(self):
        packet = prepare_atomic_review(self.run)
        for refs in ([SID + "@snippet"], [], None, [[]]):
            changed = copy.deepcopy(packet)
            changed["units"][0]["claims"][0]["citation_refs"] = refs
            self.assertFalse(check_review(self.run, changed)["valid"])
        packet["units"][0]["claims"][0].pop("citation_refs")
        self.assertFalse(check_review(self.run, packet)["valid"])

    def test_correct_snippet_review_and_old_packets_remain_valid_without_human_score(self):
        for citation in (f"[{SID}@snippet]", f"[{SID}]"):
            run = {**self.run, "answer": "保留 7 天。" + citation}
            packet = prepare_atomic_review(run)
            packet["reviewer"] = {"kind": "assistant", "name": "test fixture"}
            packet.update(coverage_checked=True, atomicity_checked=True)
            packet["units"][0]["claims"][0].update(
                label="supported",
                reason="Quote matches the selected layer",
                evidence=[{"source_id": SID, "kind": "snippet", "quote": "保留 7 天"}],
            )
            self.assertTrue(check_review(run, packet)["valid"])
            self.assertIsNone(check_review(run, packet)["human_support_label_rate"])


class EvidenceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_fixed_cases_keep_immutable_snippet_and_separate_page(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                url = "https://example.org/" + case["id"]
                sid = source_id(url)

                async def search(query, limit):
                    return [Source("Synthetic case", url, case["snippet"])]

                async def reader(url, limit, focus=""):
                    if "page_error" in case:
                        raise PageReadError(case["page_error"])
                    return case["page"]

                class CaseModel(ScriptedModel):
                    async def _call_api(self, *args, **kwargs):
                        self.calls += 1
                        if self.calls < 3:
                            name = "search_web" if self.calls == 1 else "read_page"
                            inputs = (
                                {"query": "retention"} if self.calls == 1 else {"source_id": sid}
                            )
                            block = ToolCallBlock(
                                id=str(self.calls), name=name, input=json.dumps(inputs)
                            )
                        else:
                            # Deliberately false page reference when reading fails.
                            block = TextBlock(text=f"预置控制流文本 [{sid}@snippet] [{sid}@page]")
                        return ChatResponse(content=[block], is_last=True)

                result = await run_research(
                    case["topic"],
                    CaseModel(),
                    search,
                    ResearchConfig(max_searches=1, max_page_reads=1),
                    reader=reader,
                    mode="offline_fixture",
                )
                self.assertEqual(result["sources"][0]["content"], case["snippet"])
                self.assertEqual(result["sources"][0]["evidence_kind"], "search_snippet")
                self.assertEqual(result["metrics"]["tool_invocations"], 2)
                if "page" in case:
                    self.assertEqual(result["page_evidence"][0]["content"], case["page"])
                    self.assertEqual(result["page_evidence"][0]["evidence_kind"], "page_excerpt")
                    self.assertTrue(result["metrics"]["evidence_levels"]["contract_valid"])
                else:
                    self.assertEqual(result["page_evidence"], [])
                    self.assertEqual(
                        result["metrics"]["evidence_levels"]["unavailable_references"],
                        [sid + "@page"],
                    )
                    self.assertIn("未保存正文片段", render_report(result))
                packet = prepare_atomic_review(result)
                self.assertTrue(check_review(result, packet)["valid"])

    async def test_empty_page_is_not_a_success_or_citable_evidence(self):
        async def search(query, limit):
            return [Source("test", "https://example.org/test", "snippet")]

        async def reader(*args, **kwargs):
            return " "

        session = ResearchSession(ResearchConfig(), search, reader)
        await session.search_web("query")
        chunk = await session.read_page(source_id("https://example.org/test"))
        payload = json.loads(chunk.content[0].text)
        self.assertEqual(payload["error"], "empty_page_excerpt")
        self.assertNotIn("citation", payload)
        self.assertEqual(session.page_evidence, {})
