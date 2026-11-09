"""Two separately anchored claim drafts and explicit many-to-many revision mappings.

No automatic semantic alignment, fidelity score or promotion to human labels.
"""

import json
from html import escape

from .atomic_review import prepare_atomic_review
from .review import check_review, fingerprint
from .revision import revision_integrity_errors

RELATIONS = {"pending", "preserved", "changed", "omitted", "added", "not_factual"}


def candidate_for_review(revision: dict) -> str | None:
    # Incomplete responses may contain an adapter's placeholder, or just partial
    # text. Preserve that raw text for diagnosis, but do not draft claims from it.
    incomplete = {
        "revision_completion_unverified",
        "revision_no_response",
        "revision_timeout",
        "revision_model_error",
    }
    return (
        None
        if incomplete.intersection(revision.get("errors", []))
        else revision.get("candidate_answer")
    )


def review_view(run: dict, answer: str) -> dict:
    """A review-only view, not a new research run with inherited execution metrics."""
    return {
        "answer": answer,
        "sources": run.get("sources", []),
        "page_evidence": run.get("page_evidence", []),
    }


def claim_index(packet: dict | None) -> dict:
    return {
        claim["claim_id"]: claim
        for unit in (packet or {}).get("units", [])
        for claim in unit["claims"]
    }


def prepare_comparison(run: dict, revision: dict, checklist: list[str] | None = None) -> dict:
    if revision.get("original_answer") != run["answer"] or revision_integrity_errors(revision):
        raise ValueError("Revision does not match the saved original")
    original = prepare_atomic_review(review_view(run, run["answer"]))
    candidate_text = candidate_for_review(revision)
    candidate = (
        prepare_atomic_review(review_view(run, candidate_text))
        if isinstance(candidate_text, str)
        else None
    )
    return {
        "kind": "revision_comparison",
        "schema_version": 1,
        "source_run_fingerprint": fingerprint(run),
        "revision_fingerprint": fingerprint(revision),
        "reviewer": {"kind": "unassigned", "name": ""},
        "mapping_coverage_checked": False,
        "review_checklist": checklist or [],
        "original": original,
        "candidate": candidate,
        "mappings": [
            {
                "mapping_id": f"M{i:03}",
                "original": [cid],
                "candidate": [],
                "relation": "pending",
                "reason": "",
            }
            for i, cid in enumerate(claim_index(original), 1)
        ],
        "note": "Separate Unicode offsets per answer. Initial claims are lines, not verified atomic facts. Empty candidate links mean not yet mapped, NOT proven omissions. Edit JSON after review; many-to-many mappings are allowed. No automatic quality score.",
    }


def check_comparison(run: dict, revision: dict, packet: dict) -> dict:
    errors, checks = [], {}
    try:
        if packet.get("kind") != "revision_comparison" or packet.get("schema_version") != 1:
            errors.append("comparison_schema_mismatch")
        if packet.get("source_run_fingerprint") != fingerprint(run) or packet.get(
            "revision_fingerprint"
        ) != fingerprint(revision):
            errors.append("comparison_fingerprint_mismatch")
        if revision.get("original_answer") != run["answer"] or revision_integrity_errors(revision):
            errors.append("revision_original_or_integrity_mismatch")
        for side, answer in (
            ("original", run["answer"]),
            ("candidate", candidate_for_review(revision)),
        ):
            if answer is None:
                if packet.get(side) is not None:
                    errors.append(side + "_unexpected_packet")
                checks[side] = None
            else:
                checks[side] = check_review(review_view(run, answer), packet[side])
                if not checks[side]["valid"]:
                    errors.extend(side + ":" + e for e in checks[side]["errors"])
        claims = {side: claim_index(packet.get(side)) for side in ("original", "candidate")}
        reviewer = packet["reviewer"]
        if reviewer.get("kind") not in {"unassigned", "assistant", "human"} or (
            reviewer["kind"] != "unassigned" and not reviewer.get("name", "").strip()
        ):
            errors.append("invalid_mapping_reviewer")
        mapped = {"original": set(), "candidate": set()}
        ids, pending = set(), 0
        if not isinstance(packet["mappings"], list):
            raise TypeError
        for mapping in packet["mappings"]:
            mid = mapping["mapping_id"]
            if not isinstance(mid, str) or not mid.strip() or mid in ids:
                errors.append("invalid_or_duplicate_mapping_id")
            ids.add(mid)
            relation = mapping["relation"]
            if relation not in RELATIONS:
                errors.append("invalid_mapping_relation")
            for side in claims:
                refs = mapping[side]
                if not isinstance(refs, list) or any(not isinstance(cid, str) for cid in refs):
                    raise TypeError
                if len(set(refs)) != len(refs) or any(cid not in claims[side] for cid in refs):
                    errors.append(side + "_invalid_claim_link")
                if relation != "pending":
                    mapped[side].update(refs)
            left, right = bool(mapping["original"]), bool(mapping["candidate"])
            if not (left or right):
                errors.append("empty_mapping")
            if relation in {"preserved", "changed"} and not (left and right):
                errors.append("paired_relation_requires_both_sides")
            if relation == "omitted" and (not left or right):
                errors.append("omitted_requires_original_only")
            if relation == "added" and (left or not right):
                errors.append("added_requires_candidate_only")
            if relation == "pending":
                pending += 1
            elif reviewer["kind"] == "unassigned" or not mapping.get("reason", "").strip():
                errors.append("mapping_reviewer_or_reason_missing")
        unresolved = {side: sorted(set(claims[side]) - mapped[side]) for side in claims}
        if type(packet.get("mapping_coverage_checked")) is not bool:
            errors.append("invalid_mapping_coverage_flag")
        if packet.get("mapping_coverage_checked") and (
            pending
            or any(unresolved.values())
            or reviewer["kind"] == "unassigned"
            or packet.get("candidate") is None
        ):
            errors.append("mapping_coverage_claim_incomplete")
        return {
            "valid": not errors,
            "errors": errors,
            "pending_mappings": pending,
            "unresolved_claims": unresolved,
            "side_checks": checks,
            "review_complete": not errors
            and packet.get("mapping_coverage_checked") is True
            and all(check and check["review_complete"] for check in checks.values()),
            "fidelity_score": None,
            "note": "Bookkeeping only. Unresolved is not a semantic omission; even complete labels do not certify truth or reviewer identity.",
        }
    except (KeyError, TypeError, AttributeError, ValueError):
        return {
            "valid": False,
            "errors": errors + ["malformed_comparison"],
            "review_complete": False,
            "fidelity_score": None,
        }


def render_comparison(run: dict, revision: dict, packet: dict) -> str:
    """Self-contained read-only HTML; escape all saved text, no JS or remote assets."""
    check = check_comparison(run, revision, packet)
    if not check["valid"]:
        raise ValueError("Invalid comparison packet")

    def literal(value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        return "<pre>" + escape(text) + "</pre>"

    parts = [
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>缩稿对照复核</title><style>
body{font:16px/1.6 system-ui,sans-serif;margin:32px auto;padding:0 20px;max-width:1440px;color:#172c40;background:#f7f9fc}
h1,h2,h3{line-height:1.3}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.65 ui-monospace,monospace;margin:0}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:16px 0}.card,details{padding:18px;border:1px solid #cbd5df;border-radius:8px;background:white;margin:12px 0;min-width:0}
.notice{border-left:5px solid #b76c18;padding:12px;background:#fff4df}.muted{color:#516376}summary{cursor:pointer;font-weight:600}
@media(max-width:760px){.pair{grid-template-columns:1fr}body{margin:16px auto}}
</style><body><h1>缩稿对照复核</h1>
<p class="notice">只读材料。候选不一定被采用；原稿本身也可能有错误。空映射表示尚未对应，不能自动判为遗漏。初稿按非空行生成，复合事实需要人工拆分。修改 JSON 后再检查/导出，不会自动写 human 或产生保真评分。</p>"""
    ]
    parts += [
        "<h2>本次状态与预先核对要点</h2>",
        literal(
            {
                "revision_status": revision["status"],
                "applied": revision["applied"],
                "source_run_fingerprint": packet["source_run_fingerprint"],
                "review_checklist": packet["review_checklist"],
            }
        ),
    ]
    parts += [
        '<div class="pair"><section class="card"><h2>原稿</h2>',
        literal(run["answer"]),
        '</section><section class="card"><h2>候选稿</h2>',
        '<p class="notice">未产生完整候选，下列文本仅用于故障诊断，不生成候选声明，也不标记原稿内容被删除。</p>'
        if candidate_for_review(revision) is None
        else "",
        literal(
            revision.get("candidate_answer")
            if revision.get("candidate_answer") is not None
            else "未产生候选；不据此认定所有声明被删除。"
        ),
        "</section></div>",
    ]
    indexes = {side: claim_index(packet.get(side)) for side in ("original", "candidate")}
    parts += ["<h2>声明对应（人工填写，允许多对多）</h2>"]
    for mapping in packet["mappings"]:
        parts += [
            '<section class="card"><h3>' + escape(mapping["mapping_id"]) + "</h3>",
            literal({k: mapping[k] for k in ("relation", "reason")}),
            '<div class="pair">',
        ]
        for side, label in (("original", "原稿声明"), ("candidate", "候选声明")):
            parts += ["<section><h3>" + label + "</h3>"]
            for cid in mapping[side]:
                claim = indexes[side][cid]
                parts += [
                    "<h4>" + escape(cid) + "</h4>",
                    literal(claim["statement"]),
                    "<details><summary>位置、引用与证据</summary>",
                    literal(claim),
                    "</details>",
                ]
            if not mapping[side]:
                parts += [literal("无链接；看 relation 区分待对应与人工标记的遗漏/新增。")]
            parts += ["</section>"]
        parts += ["</div></section>"]
    parts += ["<h2>尚未完成对应的声明</h2>"]
    for side in indexes:
        parts += [
            "<details open><summary>" + side + "</summary>",
            literal([indexes[side][cid] for cid in check["unresolved_claims"][side]]),
            "</details>",
        ]
    parts += ["<h2>两层保存证据（各自核对，未重新抓取）</h2>"]
    for kind, items in (
        ("snippet", run.get("sources", [])),
        ("page", run.get("page_evidence", [])),
    ):
        for item in items:
            parts += [
                "<details><summary>" + escape(item["id"] + "@" + kind) + "</summary>",
                literal(item),
                "</details>",
            ]
    parts += ["<h2>机械检查</h2>", literal(check), "</body></html>"]
    return "\n".join(parts)
