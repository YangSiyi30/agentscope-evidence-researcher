"""Editable atomic-claim annotations anchored to an immutable saved answer.

The validator checks bookkeeping, not atomicity, entailment or reviewer identity.
Offsets are Python Unicode code points in the original answer, end-exclusive.
"""

import re
from collections import Counter

from .evidence import citation_ids, quote_evidence_text, supported_reference_errors, typed_refs
from .review import LABELS, fingerprint

OFFSET_BASIS = "answer_unicode_codepoints_end_exclusive"


def answer_units(answer: str) -> list[dict]:
    """Keep every nonblank line, including headings and table/Markdown syntax."""
    units = []
    for match in re.finditer(r"[^\r\n]+", answer):
        if match.group().strip():
            units.append(
                {
                    "unit_id": f"C{len(units) + 1:03}",
                    "start": match.start(),
                    "end": match.end(),
                    "text": match.group(),
                    "citation_ids": citation_ids(match.group()),
                    **(
                        {"citation_refs": typed_refs(match.group())}
                        if typed_refs(match.group())
                        else {}
                    ),
                }
            )
    return units


def prepare_atomic_review(run: dict) -> dict:
    units = []
    for unit in answer_units(run["answer"]):
        units.append(
            {
                **unit,
                "claims": [
                    {
                        "claim_id": unit["unit_id"] + ".A01",
                        "statement": unit["text"],
                        "spans": [{k: unit[k] for k in ("start", "end", "text")}],
                        "citation_ids": unit["citation_ids"],
                        **(
                            {"citation_refs": unit["citation_refs"]}
                            if "citation_refs" in unit
                            else {}
                        ),
                        "overlap_reason": "",
                        "label": "pending",
                        "evidence": [],
                        "reason": "",
                    }
                ],
            }
        )
    return {
        "schema_version": 2,
        "run_sha256": fingerprint(run),
        "answer": run["answer"],
        "offset_basis": OFFSET_BASIS,
        "reviewer": {"kind": "unassigned", "name": ""},
        "coverage_checked": False,
        "atomicity_checked": False,
        "note": "One draft per nonblank line, NOT pre-verified atomic claims. Split compound facts, retain all text, explain shared spans and non-factual labels. Citations may be selected only from the parent line; scope still requires review.",
        "units": units,
    }


def _nonblank(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_evidence(run: dict, claim: dict, label: str) -> list[str]:
    errors = []
    evidence = claim.get("evidence")
    if not isinstance(evidence, list) or any(not isinstance(e, dict) for e in evidence):
        return ["invalid_evidence"]
    if label in {"supported", "contradicted"} and not evidence:
        errors.append("missing_evidence")
    matched_ids = set()
    matched_pairs = set()
    for item in evidence:
        sid, quote = item.get("source_id"), item.get("quote")
        if not isinstance(sid, str):
            errors.append("invalid_evidence_source")
            continue
        body = quote_evidence_text(run, item)
        if body is None:
            errors.append("invalid_evidence_location")
            body = ""
        if not _nonblank(quote) or quote not in body:
            errors.append("quote_not_in_saved_evidence")
        else:
            matched_ids.add(sid)
            matched_pairs.add((sid, item["kind"]))
    if label == "supported" and not any(
        sid in matched_ids for sid in claim.get("citation_ids", []) if isinstance(sid, str)
    ):
        errors.append("support_has_no_matching_answer_citation")
    if label == "supported":
        refs = claim.get("citation_refs", [])
        if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
            errors.extend(supported_reference_errors(refs, matched_pairs))
    return errors


def check_atomic_review(run: dict, packet: dict) -> dict:
    errors, overlaps = [], []
    answer = run["answer"]
    expected = answer_units(answer)
    if packet.get("schema_version") != 2 or packet.get("run_sha256") != fingerprint(run):
        errors.append("run_fingerprint_mismatch")
    if packet.get("answer") != answer or packet.get("offset_basis") != OFFSET_BASIS:
        errors.append("answer_or_offset_basis_changed")
    reviewer = packet.get("reviewer")
    kind = reviewer.get("kind") if isinstance(reviewer, dict) else None
    if not isinstance(kind, str) or kind not in {"unassigned", "assistant", "human"}:
        errors.append("invalid_reviewer_kind")
    if (
        isinstance(kind, str)
        and kind in {"assistant", "human"}
        and not _nonblank(reviewer.get("name"))
    ):
        errors.append("reviewer_name_missing")
    units = packet.get("units")
    if not isinstance(units, list) or any(not isinstance(u, dict) for u in units):
        return {
            "valid": False,
            "errors": errors + ["invalid_units"],
            "human_support_label_rate": None,
        }
    if [u.get("unit_id") for u in units] != [u["unit_id"] for u in expected]:
        errors.append("missing_reordered_or_extra_units")
    originals = {u["unit_id"]: u for u in expected}
    counts = Counter()
    covered = set()
    claim_ids = set()
    for unit in units:
        uid = unit.get("unit_id")
        original = originals.get(uid) if isinstance(uid, str) else None
        if original is None or any(unit.get(key) != value for key, value in original.items()):
            errors.append(f"{uid}:unit_changed")
            continue
        claims = unit.get("claims")
        if (
            not isinstance(claims, list)
            or not claims
            or any(not isinstance(c, dict) for c in claims)
        ):
            errors.append(f"{uid}:invalid_claims")
            continue
        span_sets = []
        for claim in claims:
            cid = claim.get("claim_id")
            if (
                not isinstance(cid, str)
                or not re.fullmatch(re.escape(uid) + r"\.A[0-9]+", cid)
                or cid in claim_ids
            ):
                errors.append(f"{uid}:invalid_or_duplicate_claim_id")
            else:
                claim_ids.add(cid)
            prefix = str(cid)
            if not _nonblank(claim.get("statement")):
                errors.append(f"{prefix}:empty_statement")
            ids = claim.get("citation_ids")
            if not isinstance(ids, list) or any(
                not isinstance(sid, str) or sid not in original["citation_ids"] for sid in ids
            ):
                errors.append(f"{prefix}:citation_outside_parent_unit")
                ids = []
            if "citation_refs" in original:
                refs = claim.get("citation_refs")
                if not isinstance(refs, list) or any(
                    not isinstance(ref, str) or ref not in original["citation_refs"] for ref in refs
                ):
                    errors.append(f"{prefix}:citation_reference_outside_parent_unit")
                elif (set(ids) & {ref.split("@", 1)[0] for ref in original["citation_refs"]}) != {
                    ref.split("@", 1)[0] for ref in refs
                }:
                    errors.append(f"{prefix}:citation_reference_ids_mismatch")
            spans = claim.get("spans")
            positions = set()
            if not isinstance(spans, list) or not spans:
                errors.append(f"{prefix}:missing_spans")
                spans = []
            for span in spans:
                if not isinstance(span, dict):
                    errors.append(f"{prefix}:invalid_span")
                    continue
                start, end = span.get("start"), span.get("end")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not original["start"] <= start < end <= original["end"]
                ):
                    errors.append(f"{prefix}:span_out_of_bounds")
                    continue
                if span.get("text") != answer[start:end] or not answer[start:end].strip():
                    errors.append(f"{prefix}:span_text_mismatch_or_blank")
                    continue
                current = {i for i in range(start, end) if not answer[i].isspace()}
                if positions & current:
                    errors.append(f"{prefix}:overlapping_spans_within_claim")
                positions.update(current)
            covered.update(positions)
            for previous, other in span_sets:
                if positions & other:
                    overlaps.append([previous.get("claim_id"), cid])
                    if not _nonblank(previous.get("overlap_reason")) or not _nonblank(
                        claim.get("overlap_reason")
                    ):
                        errors.append(f"{prefix}:unexplained_shared_span")
            span_sets.append((claim, positions))
            label = claim.get("label")
            if not isinstance(label, str) or label not in LABELS:
                errors.append(f"{prefix}:invalid_label")
                continue
            counts[label] += 1
            errors.extend(
                f"{prefix}:{e}" for e in _check_evidence(run, {**claim, "citation_ids": ids}, label)
            )
            if label != "pending" and (not _nonblank(claim.get("reason")) or kind == "unassigned"):
                errors.append(f"{prefix}:reviewer_or_reason_missing")
    required = {i for i, char in enumerate(answer) if not char.isspace()}
    missing = sorted(required - covered)
    if missing:
        errors.append("uncovered_answer_characters")
    factual = sum(counts[k] for k in ("supported", "contradicted", "insufficient", "uncited"))
    complete = (
        bool(counts)
        and not counts["pending"]
        and not errors
        and packet.get("coverage_checked") is True
        and packet.get("atomicity_checked") is True
    )
    return {
        "valid": not errors,
        "errors": errors,
        "reviewer_kind": kind,
        "counts": dict(counts),
        "total_claims": sum(counts.values()),
        "reviewed_claims": sum(counts.values()) - counts["pending"],
        "covered_nonblank_chars": len(required & covered),
        "total_nonblank_chars": len(required),
        "uncovered_offsets": missing,
        "shared_span_pairs": overlaps,
        "review_complete": complete,
        "human_support_label_rate": counts["supported"] / factual
        if complete and kind == "human" and factual
        else None,
        "note": "Coverage is mechanical, not semantic atomicity or entailment. Statement fidelity, citation scope, reviewer identity and atomicity are human declarations. Assistant judgments are not gold.",
    }
