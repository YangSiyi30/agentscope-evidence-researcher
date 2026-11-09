"""Review packets bind judgments to saved evidence. They do not decide factual truth."""

import hashlib
import json
import re
from collections import Counter

from .evidence import citation_ids, quote_evidence_text, supported_reference_errors, typed_refs

LABELS = {"pending", "supported", "contradicted", "insufficient", "uncited", "not_factual"}


def fingerprint(run: dict) -> str:
    return hashlib.sha256(json.dumps(run, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def candidate_units(answer: str) -> list[dict]:
    units = []
    for line in answer.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or re.fullmatch(r"[| :\-]+", line):
            continue
        for match in re.finditer(r"[^。！？]+[。！？]?(?:\s*\[SRC-[A-Za-z0-9@]+\])*", line):
            text = match.group().strip()
            if text:
                units.append(
                    {
                        "unit_id": f"C{len(units) + 1:03}",
                        "text": text,
                        "citation_ids": citation_ids(text),
                    }
                )
    return units


def prepare_review(run: dict) -> dict:
    return {
        "schema_version": 1,
        "run_sha256": fingerprint(run),
        "reviewer": {"kind": "unassigned", "name": ""},
        "coverage_checked": False,
        "note": "Heuristic sentence/line units, not verified atomic claims. Labels require evidence review. No automatic truth score.",
        "units": [
            {**unit, "label": "pending", "evidence": [], "reason": ""}
            for unit in candidate_units(run["answer"])
        ],
    }


def check_review(run: dict, packet: dict) -> dict:
    if not isinstance(packet, dict):
        return {"valid": False, "errors": ["invalid_packet"]}
    if packet.get("schema_version") == 2:
        from .atomic_review import check_atomic_review

        return check_atomic_review(run, packet)
    errors = []
    expected = candidate_units(run["answer"])
    if packet.get("schema_version") != 1 or packet.get("run_sha256") != fingerprint(run):
        errors.append("run_fingerprint_mismatch")
    reviewer = packet.get("reviewer", {})
    if not isinstance(reviewer, dict):
        return {"valid": False, "errors": ["invalid_reviewer"]}
    kind = reviewer.get("kind")
    if not isinstance(kind, str):
        return {"valid": False, "errors": ["invalid_reviewer_kind"]}
    if kind not in {"unassigned", "assistant", "human"}:
        errors.append("invalid_reviewer_kind")
    units = packet.get("units", [])
    if not isinstance(units, list):
        return {"valid": False, "errors": ["invalid_units"]}
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get("unit_id"), str):
            return {"valid": False, "errors": ["invalid_unit"]}
        if not isinstance(unit.get("label"), str) or not isinstance(unit.get("reason", ""), str):
            return {"valid": False, "errors": ["invalid_unit_fields"]}
        ids = unit.get("citation_ids")
        if not isinstance(ids, list) or any(not isinstance(sid, str) for sid in ids):
            return {"valid": False, "errors": ["invalid_unit_fields"]}
        evidence = unit.get("evidence", [])
        if not isinstance(evidence, list) or any(not isinstance(item, dict) for item in evidence):
            return {"valid": False, "errors": ["invalid_evidence"]}
        if any(not isinstance(item.get("source_id"), str) for item in evidence):
            return {"valid": False, "errors": ["invalid_evidence_source"]}
    if [u.get("unit_id") for u in units] != [u["unit_id"] for u in expected]:
        errors.append("missing_reordered_or_extra_units")
    texts = {u["unit_id"]: u for u in expected}
    counts = Counter()
    for unit in units:
        identity = unit.get("unit_id")
        original = texts.get(identity, {})
        if unit.get("text") != original.get("text") or unit.get("citation_ids") != original.get(
            "citation_ids"
        ):
            errors.append(f"{identity}:unit_changed")
        label = unit.get("label")
        if label not in LABELS:
            errors.append(f"{identity}:invalid_label")
            continue
        counts[label] += 1
        evidence = unit.get("evidence", [])
        if label in {"supported", "contradicted"} and not evidence:
            errors.append(f"{identity}:missing_evidence")
        matched_pairs = set()
        for item in evidence:
            sid, quote = item.get("source_id"), item.get("quote")
            level = item.get("kind")
            body = quote_evidence_text(run, item)
            if body is None:
                errors.append(f"{identity}:invalid_evidence_location")
                body = ""
            if not isinstance(quote, str) or not quote.strip() or quote not in body:
                errors.append(f"{identity}:quote_not_in_saved_evidence")
            else:
                matched_pairs.add((sid, level))
        if label == "supported":
            errors.extend(
                f"{identity}:{error}"
                for error in supported_reference_errors(
                    typed_refs(original.get("text", "")), matched_pairs
                )
            )
        if label == "supported" and not (
            set(unit.get("citation_ids", [])) & {e.get("source_id") for e in evidence}
        ):
            errors.append(f"{identity}:support_has_no_matching_answer_citation")
        if label != "pending" and (not unit.get("reason", "").strip() or kind == "unassigned"):
            errors.append(f"{identity}:reviewer_or_reason_missing")
    reviewed = len(units) - counts["pending"]
    factual = sum(counts[k] for k in ("supported", "contradicted", "insufficient", "uncited"))
    human_complete = (
        kind == "human"
        and packet.get("coverage_checked") is True
        and not counts["pending"]
        and not errors
    )
    return {
        "valid": not errors,
        "errors": errors,
        "reviewer_kind": kind,
        "counts": dict(counts),
        "reviewed_units": reviewed,
        "total_units": len(units),
        "human_support_label_rate": counts["supported"] / factual
        if human_complete and factual
        else None,
        "note": "Quote matching is mechanical, not entailment. Human identity/coverage is self-declared; assistant labels are not gold. Unit splitting can miss or merge claims.",
    }
