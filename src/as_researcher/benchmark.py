"""Task rubrics, frozen evidence and paired summaries, outside the Agent loop.

Mechanical checks validate bindings and quotes. A reviewer still decides meaning.
No model or network is constructed by this module.
"""

import hashlib
import json
import math
import re
import secrets
import statistics
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .core import canonical_url
from .evidence import quote_evidence_text
from .pages import PageReadError, TextExtractor, select_excerpt
from .review import fingerprint
from .segments import parse_document, select_passages

STRATEGIES = ("head", "window", "segments", "bm25", "balanced")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def source_digest() -> str:
    return digest(
        b"".join(
            p.name.encode() + b"\0" + p.read_bytes()
            for p in sorted(Path(__file__).parent.glob("*.py"))
        )
    )


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    dimension: Literal["coverage", "support", "analysis", "instruction", "presentation"]
    description: str = Field(min_length=8, max_length=1000)
    required: bool = True
    improvement: str = Field(min_length=4, max_length=500)


class ResearchTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    category: str = Field(min_length=2, max_length=80)
    question: str = Field(min_length=10, max_length=2000)
    documents: list[str] = Field(max_length=3)
    evidence_required: bool = True
    max_answer_chars: int = Field(default=500, ge=100, le=10000)
    required_exposure: str | None = Field(default=None, min_length=8, max_length=200)
    rubric: list[Criterion] = Field(min_length=2, max_length=12)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({r.id for r in self.rubric}) != len(self.rubric) or not any(
            r.required for r in self.rubric
        ):
            raise ValueError("Rubric IDs must be unique with at least one required criterion")
        if len(set(self.documents)) != len(self.documents):
            raise ValueError("Duplicate task documents")
        return self


class ResearchSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    name: str
    provenance: str
    documents: dict[str, str] = Field(max_length=8)
    synthetic_documents: dict[str, str] = Field(default_factory=dict, max_length=3)
    tasks: list[ResearchTask] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_relations(self):
        if len({t.id for t in self.tasks}) != len(self.tasks):
            raise ValueError("Duplicate task IDs")
        for name, url in self.documents.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name) or canonical_url(url) != url:
                raise ValueError("Invalid document identity or canonical public URL")
        if any(d not in self.documents for task in self.tasks for d in task.documents):
            raise ValueError("Unknown document")
        if any(
            key not in self.documents or len(html) > 10000
            for key, html in self.synthetic_documents.items()
        ):
            raise ValueError("Invalid synthetic document")
        return self


def load_suite(path: Path) -> ResearchSuite:
    if path.stat().st_size > 256_000:
        raise ValueError("Suite too large")
    return ResearchSuite.model_validate_json(path.read_bytes())


def load_corpus(folder: Path, documents: dict[str, str]) -> dict[str, str]:
    """Require byte-exact snapshots. No missing page/network fallback or path input."""
    folder = folder.resolve()
    manifest = json.loads((folder / "manifest.json").read_bytes())
    pages = {}
    for name, url in documents.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
            raise ValueError("Invalid snapshot ID")
        path = (folder / (name + ".html")).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or path.stat().st_size > 1_000_000:
            raise ValueError("Missing/oversize snapshot")
        raw = path.read_bytes()
        record = manifest.get("pages", {}).get(name, {})
        if record.get("url") != url or record.get("sha256") != digest(raw):
            raise ValueError("Snapshot fingerprint/URL mismatch")
        pages[url] = raw.decode("utf-8")
    return pages


def extract_frozen(html: str, strategy: str, focus: str, budget: int) -> dict:
    if strategy not in STRATEGIES or not 100 <= budget <= 12000:
        raise ValueError("Invalid strategy/budget")
    if strategy in {"segments", "bm25", "balanced"}:
        return select_passages(
            parse_document(html),
            focus,
            budget,
            ranking="lexical" if strategy == "segments" else strategy,
        )
    parser = TextExtractor()
    parser.feed(html)
    text = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", "".join(parser.parts))).strip()
    return {
        "content": text[:budget] if strategy == "head" else select_excerpt(text, focus, budget),
        "strategy": strategy,
        "is_full_document": False,
    }


class FrozenReader:
    """Same excerpt code, byte-bound HTML, no page network; real model is separate."""

    mode = "snapshot"

    def __init__(self, pages: dict[str, str], strategy: str):
        self.pages, self.strategy = pages, strategy

    async def __call__(self, url: str, max_chars: int, focus: str = "") -> dict:
        if url not in self.pages:
            raise PageReadError("invalid_url")
        result = extract_frozen(self.pages[url], self.strategy, focus, max_chars)
        if not result["content"]:
            raise PageReadError("excerpt_budget_insufficient")
        return result


def prepare_task_review(run: dict, task: ResearchTask) -> dict:
    return {
        "schema_version": 1,
        "kind": "research_task_review",
        "run_sha256": fingerprint(run),
        "task_sha256": digest(task.model_dump_json().encode()),
        "task_id": task.id,
        "reviewer": {"kind": "unassigned", "name": ""},
        "coverage_checked": False,
        "note": "Judge ONLY saved answer/evidence. Quotes are locators, not automatic entailment. Assistant is not independent human gold. Atomic claim precision requires the separate atomic review.",
        "criteria": [
            {"id": c.id, "met": None, "answer_quote": "", "evidence": [], "reason": ""}
            for c in task.rubric
        ],
    }


def check_task_review(run: dict, task: ResearchTask, packet: dict) -> dict:
    errors = []
    if not isinstance(packet, dict):
        return {
            "valid": False,
            "complete": False,
            "semantic_pass": None,
            "errors": ["invalid_packet"],
        }
    expected = prepare_task_review(run, task)
    for key in ("schema_version", "kind", "run_sha256", "task_sha256", "task_id"):
        if packet.get(key) != expected[key]:
            errors.append(key + "_mismatch")
    reviewer = packet.get("reviewer")
    kind = reviewer.get("kind") if isinstance(reviewer, dict) else None
    if kind not in ("unassigned", "assistant", "human"):
        errors.append("invalid_reviewer")
    if kind in ("assistant", "human") and not str(reviewer.get("name", "")).strip():
        errors.append("missing_reviewer_name")
    rows = packet.get("criteria")
    if not isinstance(rows, list) or not all(isinstance(c, dict) for c in rows):
        return {
            "valid": False,
            "complete": False,
            "semantic_pass": None,
            "errors": errors + ["invalid_criteria"],
        }
    if [c.get("id") for c in rows] != [c.id for c in task.rubric]:
        errors.append("missing_reordered_or_extra_criteria")
    assessed = []
    for criterion, row in zip(task.rubric, rows):
        prefix = criterion.id + ":"
        value, quote = row.get("met"), row.get("answer_quote")
        if value is not None and type(value) is not bool:
            errors.append(prefix + "invalid_label")
        if not isinstance(quote, str) or (quote and quote not in run["answer"]):
            errors.append(prefix + "answer_quote_mismatch")
        if value is not None:
            if (
                kind == "unassigned"
                or not isinstance(row.get("reason"), str)
                or not row["reason"].strip()
            ):
                errors.append(prefix + "reviewer_or_reason_missing")
            if value is True and not quote:
                errors.append(prefix + "positive_requires_answer_quote")
            assessed.append(row)
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not all(isinstance(e, dict) for e in evidence):
            errors.append(prefix + "invalid_evidence")
            continue
        if value is True and criterion.dimension == "support" and not evidence:
            errors.append(prefix + "support_requires_saved_evidence")
        for item in evidence:
            body = quote_evidence_text(run, item)
            if (
                not isinstance(item.get("quote"), str)
                or not item["quote"].strip()
                or body is None
                or item["quote"] not in body
            ):
                errors.append(prefix + "quote_not_in_saved_evidence")
    complete = (
        not errors and len(assessed) == len(task.rubric) and packet.get("coverage_checked") is True
    )
    failed = [c.id for c, r in zip(task.rubric, rows) if c.required and r.get("met") is False]
    return {
        "valid": not errors,
        "errors": errors,
        "complete": complete,
        "reviewer_kind": kind,
        "semantic_pass": not failed if complete else None,
        "failed_required": failed,
        "assessed": len(assessed),
        "total": len(task.rubric),
        "improvements": [c.improvement for c in task.rubric if c.id in failed],
    }


def operational_checks(run: dict, task: ResearchTask) -> dict:
    """Outcome/resource gates; avoid demanding one exact valid tool trajectory."""
    m, config = run["metrics"], run["config"]
    levels = m["evidence_levels"]
    checks = {
        "completed": m["status"] == "completed",
        "search_finished_without_error": m.get("search_stop_reason") != "search_error",
        "answer_within_limit": m["answer_within_limit"] is True,
        "task_answer_within_limit": len(re.sub(r"\s", "", run["answer"])) <= task.max_answer_chars,
        "search_budget": m["search_attempts"] <= config["max_searches"],
        "page_budget": m["page_read_attempts"] <= config["max_page_reads"],
        "saved_evidence": not task.evidence_required
        or bool(run.get("page_evidence"))
        or any(s.get("content") for s in run.get("sources", [])),
        "references_available": not (
            m["citations"]["invalid_ids"]
            or levels["invalid_references"]
            or levels["unavailable_references"]
            or levels["untyped_references"]
        ),
        "citations_present": not task.evidence_required or m["citations"]["has_citations"],
        "permitted_tools": all(
            e.get("tool") in {None, "search_web", "read_page"} for e in run.get("trace", [])
        ),
        "adversarial_payload_exposed": task.required_exposure is None
        or any(
            task.required_exposure in p.get("content", "") for p in run.get("page_evidence", [])
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "adversarial_marker_in_answer": bool(
            task.required_exposure and task.required_exposure in run["answer"]
        ),
        "adversarial_note": "Marker echo is diagnostic, not proof of instruction-following: a quoted attack may be legitimate analysis. Review intent and actual capabilities/trajectory.",
        "note": "No semantic correctness or citation entailment is inferred.",
    }


def trial_assessment(run: dict, task: ResearchTask, packet: dict) -> dict:
    operational = operational_checks(run, task)
    quality = check_task_review(run, task, packet)
    passed = False if not operational["passed"] else quality["semantic_pass"]
    return {"operational": operational, "quality": quality, "passed": passed}


def load_bound_batch(batch: Path) -> dict:
    """Read saved runs with the same bindings for CLI grading and terminal views."""
    batch = batch.resolve()
    plan = json.loads((batch / "plan.json").read_bytes())
    if (not isinstance(plan, dict) or not isinstance(plan.get("trials"), list)
        or not 1 <= len(plan["trials"]) <= 40
        or not all(isinstance(r, dict) and isinstance(r.get("id"), str) for r in plan["trials"])
        or len({r["id"] for r in plan["trials"]}) != len(plan["trials"])):
        raise ValueError("Invalid/empty/duplicate planned trial set")
    suite = load_suite(batch / "suite.json")
    if digest((batch / "suite.json").read_bytes()) != plan["suite_sha256"]:
        raise ValueError("Suite fingerprint mismatch")
    rows = json.loads((batch / "index.json").read_bytes())
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Invalid index rows")
    keys = ("id", "task_id", "repeat", "strategy", "answer_style", "variant")
    if [{k: r.get(k) for k in keys} for r in rows] != [
        {k: r.get(k) for k in keys} for r in plan["trials"]
    ]:
        raise ValueError("Missing/reordered/extra planned trials")
    tasks, runs = {t.id: t for t in suite.tasks}, {}
    for row in rows:
        if row.get("status") not in {"not_run", "running", "saved", "worker_failed", "process_deadline", "interrupted"}:
            raise ValueError("Invalid trial status")
        if row["task_id"] not in tasks or not re.fullmatch(r"[a-zA-Z0-9_]+", row["id"]):
            raise ValueError("Invalid trial identity")
        if row["status"] != "saved":
            continue
        folder = (batch / row["run_file"]).resolve()
        if not folder.is_relative_to(batch / row["id"]):
            raise ValueError("Run outside trial folder")
        run = json.loads((folder / "run.json").read_bytes())
        if fingerprint(run) != row["run_sha256"]:
            raise ValueError("Run fingerprint mismatch")
        if (
            run.get("benchmark", {}).get("trial_id") != row["id"]
            or run.get("benchmark", {}).get("suite_sha256") != plan["suite_sha256"]
            or run.get("provenance", {}).get("source_sha256") != plan["source_sha256"]
            or run.get("excerpt_strategy") != row["strategy"]
            or run.get("config", {}).get("answer_style", "standard")
            != row.get("answer_style", "standard")
            or run.get("config", {}).get("revise_answer", False) != plan.get("revise_answer", False)
        ):
            raise ValueError("Run does not match planned trial/source/strategy")
        runs[row["id"]] = run
    return {"plan": plan, "tasks": tasks, "rows": rows, "runs": runs}


def assess_saved_batch(batch: Path) -> dict:
    """Read-only diagnosis; no model, file writes, or semantic auto-labels."""
    context = load_bound_batch(batch)
    for row in context["rows"]:
        if row["status"] != "saved":
            row["assessment"] = {"passed": None if row["status"] == "not_run" else False}
            continue
        folder = batch / row["run_file"]
        packet_file = folder / "task-review.json"
        if not packet_file.exists():
            packet_file = folder / "task-review.draft.json"
        packet = json.loads(packet_file.read_bytes())
        run = context["runs"][row["id"]]
        row["metrics"] = run["metrics"]
        row["assessment"] = trial_assessment(run, context["tasks"][row["task_id"]], packet)
        row["review_file"] = str(packet_file)
    context["summary"] = summarize_trials(context["rows"])
    return context


def summarize_trials(rows: list[dict]) -> dict:
    """Denominators include every planned trial. Missing/unreviewed never pass.

    Empirical any/all success are complete-group descriptions, not estimates of
    public benchmark pass@k or pass^k. Repeated runs of one task are correlated.
    """
    groups = {}
    for row in rows:
        groups.setdefault(row.get("variant", row["strategy"]), []).append(row)
    summaries = {}
    for strategy, trials in groups.items():
        verdicts = [r.get("assessment", {}).get("passed") for r in trials]
        measured = [r for r in trials if isinstance(r.get("metrics"), dict)]
        durations = sorted(r["metrics"]["duration_ms"] for r in measured)
        known_usage = [
            r["metrics"]["token_usage"]
            for r in measured
            if r["metrics"].get("token_usage") is not None
        ]
        per_task = {}
        for r in trials:
            per_task.setdefault(r["task_id"], []).append(r.get("assessment", {}).get("passed"))
        summaries[strategy] = {
            "planned": len(trials),
            "runs_saved": len(measured),
            "operational_passes": sum(
                r.get("assessment", {}).get("operational", {}).get("passed") is True for r in trials
            ),
            "reviewed": sum(
                r.get("assessment", {}).get("quality", {}).get("complete") is True for r in trials
            ),
            "passed": verdicts.count(True),
            "failed": verdicts.count(False),
            "unassessed": verdicts.count(None),
            "success_rate_bounds": [
                verdicts.count(True) / len(trials),
                (verdicts.count(True) + verdicts.count(None)) / len(trials),
            ],
            "median_duration_ms": statistics.median(durations) if durations else None,
            "p95_duration_ms": durations[math.ceil(0.95 * len(durations)) - 1]
            if len(durations) >= 20
            else None,
            "usage_known_runs": len(known_usage),
            "usage_unknown_or_no_run": len(trials) - len(known_usage),
            "known_token_subtotal": {k: sum(u[k] for u in known_usage) for k in ("input", "output")}
            if known_usage
            else None,
            "empirical_tasks": {
                key: {
                    "trials": len(values),
                    "any_success": any(values) if None not in values else None,
                    "all_success": all(values) if None not in values else None,
                }
                for key, values in per_task.items()
            },
        }
    pairs = []
    if len(groups) == 2:
        left, right = groups
        indexed = {(r["task_id"], r["repeat"], r.get("variant", r["strategy"])): r for r in rows}
        for task, repeat in sorted({(r["task_id"], r["repeat"]) for r in rows}):
            a, b = indexed.get((task, repeat, left), {}), indexed.get((task, repeat, right), {})
            av, bv = a.get("assessment", {}).get("passed"), b.get("assessment", {}).get("passed")
            pairs.append(
                {
                    "task_id": task,
                    "repeat": repeat,
                    "left": left,
                    "right": right,
                    "outcome": "unassessed"
                    if av is None or bv is None
                    else "tie"
                    if av == bv
                    else "right_wins"
                    if bv
                    else "left_wins",
                }
            )
    return {
        "strategies": summaries,
        "pairs": pairs,
        "note": "Small developer-authored suite, not public leaderboard accuracy. Groups use the planned variant (strategy and optional answer style). Time includes research plus enabled revision, not setup/file writing. Null usage is unknown; token subtotals exclude unknown trials. No p95 below 20 observations. Bounds reflect unassessed outcomes, not confidence intervals.",
    }


def prepare_blind_review(samples: list[tuple[ResearchTask, dict]]) -> tuple[dict, dict]:
    """Shuffle saved samples; keep variant/run bindings in a separate private map.

    This hides explicit configuration, not every clue in style or wording.
    No reviewer labels or human identities are generated by this function.
    """
    if not 1 <= len(samples) <= 80:
        raise ValueError("Choose 1..80 saved samples")
    order = list(range(len(samples)))
    secrets.SystemRandom().shuffle(order)
    items, bindings = [], []
    for number, source_index in enumerate(order, 1):
        task, run = samples[source_index]
        identity = f"B{number:03d}"
        item = {
            "id": identity,
            "question": task.question,
            "answer": run["answer"],
            "rubric": [c.model_dump() for c in task.rubric],
            "evidence": [
                {
                    "id": p["id"],
                    "kind": kind,
                    "url": p.get("url", ""),
                    "content": p.get("content", ""),
                }
                for kind, field in (("snippet", "sources"), ("page", "page_evidence"))
                for p in run.get(field, [])
                if p.get("content", "").strip()
            ],
            "labels": [{"id": c.id, "met": None, "reason": ""} for c in task.rubric],
        }
        items.append(item)
        bindings.append(
            {
                "id": identity,
                "source_index": source_index,
                "run_sha256": fingerprint(run),
                "task_sha256": digest(task.model_dump_json().encode()),
            }
        )
    packet = {
        "schema_version": 1,
        "kind": "blind_task_review",
        "reviewer": {"kind": "unassigned", "name": ""},
        "items": items,
    }
    return packet, {
        "schema_version": 1,
        "kind": "blind_review_private_map",
        "template_sha256": fingerprint(packet),
        "bindings": bindings,
    }


def check_blind_review(template: dict, packet: dict) -> dict:
    """Verify unchanged answers/rubrics/evidence and explicit tri-state labels."""
    errors, labels = [], {}
    if not isinstance(packet, dict) or not isinstance(template, dict):
        return {"valid": False, "complete": False, "errors": ["invalid_packet"], "labels": {}}
    reviewer = packet.get("reviewer", {})
    if (
        not isinstance(reviewer, dict)
        or reviewer.get("kind") not in {"human", "assistant", "unassigned"}
        or not isinstance(reviewer.get("name"), str)
    ):
        errors.append("invalid_reviewer")
        reviewer = {}
    rows = packet.get("items", [])
    if not isinstance(rows, list) or len(rows) != len(template["items"]):
        return {"valid": False, "complete": False, "errors": ["sample_set_changed"], "labels": {}}
    if (
        packet.get("schema_version") != 1
        or packet.get("kind") != "blind_task_review"
        or set(packet) != set(template)
    ):
        errors.append("packet_contract_changed")
    for original, row in zip(template["items"], rows):
        identity = original["id"]
        if not isinstance(row, dict) or {k: v for k, v in row.items() if k != "labels"} != {
            k: v for k, v in original.items() if k != "labels"
        }:
            errors.append(identity + ":sample_changed")
            continue
        values = row.get("labels")
        if not isinstance(values, list) or [
            v.get("id") if isinstance(v, dict) else None for v in values
        ] != [v["id"] for v in original["labels"]]:
            errors.append(identity + ":criteria_changed")
            continue
        for value in values:
            key = identity + ":" + value["id"]
            if (
                set(value) != {"id", "met", "reason"}
                or (value.get("met") is not None and type(value.get("met")) is not bool)
                or not isinstance(value.get("reason"), str)
            ):
                errors.append(key + ":invalid_label")
                continue
            if value["met"] is not None and not value["reason"].strip():
                errors.append(key + ":reason_required")
            labels[key] = value["met"]
    judged = any(v is not None for v in labels.values())
    if judged and (reviewer.get("kind") == "unassigned" or not reviewer.get("name", "").strip()):
        errors.append("reviewer_required")
    return {
        "valid": not errors,
        "complete": not errors and bool(labels) and all(v is not None for v in labels.values()),
        "errors": errors,
        "labels": labels,
        "reviewer": reviewer,
    }


def review_agreement(template: dict, left: dict, right: dict) -> dict:
    """Agreement on jointly judged criteria only, not answer accuracy.

    Kappa is null for no comparable labels or degenerate expected agreement=1.
    Correlated criteria/repeats and declared identities do not certify gold.
    """
    a, b = check_blind_review(template, left), check_blind_review(template, right)
    if not a["valid"] or not b["valid"]:
        return {"valid": False, "left": a, "right": b}
    keys = [k for k in a["labels"] if a["labels"][k] is not None and b["labels"][k] is not None]
    disagreements = [k for k in keys if a["labels"][k] != b["labels"][k]]
    observed = (len(keys) - len(disagreements)) / len(keys) if keys else None
    expected = None
    if keys:
        pa = sum(a["labels"][k] for k in keys) / len(keys)
        pb = sum(b["labels"][k] for k in keys) / len(keys)
        expected = pa * pb + (1 - pa) * (1 - pb)
    kappa = (
        (observed - expected) / (1 - expected) if expected is not None and expected < 1 else None
    )
    return {
        "valid": True,
        "complete": a["complete"] and b["complete"],
        "reviewers": [a["reviewer"], b["reviewer"]],
        "declared_distinct_reviewers": a["reviewer"] != b["reviewer"],
        "total_criteria": len(a["labels"]),
        "jointly_judged": len(keys),
        "unpaired": len(a["labels"]) - len(keys),
        "agreement": observed,
        "cohen_kappa": kappa,
        "disagreements": disagreements,
        "note": "Agreement is not correctness. Names/kinds are self-declared; no identity or independence certification. Null labels excluded with denominator shown. Kappa undefined at expected agreement=1. Criteria/repeats may be correlated; no significance claim.",
    }
