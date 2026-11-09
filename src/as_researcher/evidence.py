"""Evidence-level references. Existence and quote matching are not entailment."""

import json
import math
import re

LEVEL_NAMES = {"snippet": "search_snippet", "page": "page_excerpt"}

# Reader output contributes page data, never application state or tool controls.
PAGE_READER_FIELDS = frozenset(
    {
        "content",
        "strategy",
        "selector_version",
        "document_sha256",
        "document_chars",
        "segments",
        "offset_basis",
        "is_full_document",
        "selection_warnings",
        "omitted_table_rows",
        "omitted_code_blocks",
        "omitted_code_contexts",
        "code_selection",
        "focus_allocation",
    }
)


def reader_page_data(page: dict) -> dict:
    """Validate/copy allowed JSON metadata before a page is committed to state."""
    data = {key: value for key, value in page.items() if key in PAGE_READER_FIELDS}
    string_fields = {"strategy", "selector_version", "document_sha256", "offset_basis"}
    count_fields = {
        "document_chars",
        "omitted_table_rows",
        "omitted_code_blocks",
        "omitted_code_contexts",
    }
    for key, value in data.items():
        invalid = (
            (key in string_fields and not isinstance(value, str))
            or (key in count_fields and (type(value) is not int or value < 0))
            or (key == "is_full_document" and type(value) is not bool)
            or (
                key == "selection_warnings"
                and (not isinstance(value, list) or any(not isinstance(v, str) for v in value))
            )
            or (
                key == "segments"
                and (not isinstance(value, list) or any(not isinstance(v, dict) for v in value))
            )
            or (key in {"code_selection", "focus_allocation"} and not isinstance(value, dict))
        )
        if invalid:
            raise ValueError("invalid_page_metadata")
    nodes, characters = 0, 0

    def validate(value, depth=0):
        nonlocal nodes, characters
        nodes += 1
        if nodes > 4096 or depth > 12:
            raise ValueError("invalid_page_metadata")
        if isinstance(value, str):
            characters += len(value)
        elif value is None or type(value) in (bool, int):
            pass
        elif type(value) is float and math.isfinite(value):
            pass
        elif isinstance(value, list):
            for item in value:
                validate(item, depth + 1)
        elif isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("invalid_page_metadata")
                characters += len(key)
                validate(item, depth + 1)
        else:
            raise ValueError("invalid_page_metadata")
        if characters > 128000:
            raise ValueError("invalid_page_metadata")

    validate(data)
    return json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))


def quote_evidence_text(run: dict, item: dict) -> str | None:
    """Resolve one explicit saved field, with no fallback across fields or levels.

    Missing field means content for old packets. Title is search-result metadata,
    not a page-body quote or evidence that an entire versioned page was fetched.
    None signals an invalid location; absent saved text resolves to empty text.
    """
    kind, field = item.get("kind"), item.get("field", "content")
    segment = item.get("segment_id")
    if (
        not isinstance(kind, str)
        or kind not in LEVEL_NAMES
        or not isinstance(field, str)
        or field not in {"content", "title"}
        or (field == "title" and kind != "snippet")
        or (segment is not None and not isinstance(segment, str))
        or (segment and kind != "page")
    ):
        return None
    source = next(
        (
            s
            for s in run.get("sources" if kind == "snippet" else "page_evidence", [])
            if s["id"] == item.get("source_id")
        ),
        {},
    )
    if segment:
        value = next(
            (
                s.get("text", "")
                for s in source.get("segments", [])
                if s.get("segment_id") == segment
            ),
            "",
        )
    else:
        value = source.get(field, "")
    return value if isinstance(value, str) else ""


def page_observation(page: dict) -> dict:
    """Keep detailed offsets in saved evidence, scoped code status in model input."""
    payload = dict(page)
    for key in ("segments", "document_sha256", "offset_basis", "selector_version"):
        payload.pop(key, None)
    if "code_selection" in payload:
        for key in ("omitted_code_blocks", "omitted_code_contexts"):
            payload.pop(key, None)
        payload["selection_warnings"] = [
            warning
            for warning in payload.get("selection_warnings", [])
            if warning not in {"code_blocks_omitted_for_budget", "code_context_omitted_for_budget"}
        ]
    return payload


def citation_refs(text: str) -> list[dict]:
    """Accept old SRC references for history; retain malformed new references for diagnostics."""
    refs = []
    for match in re.finditer(r"\[(SRC-[^\]\r\n]*)\]", text, flags=re.IGNORECASE):
        token = match.group(1)
        sid, separator, kind = token.partition("@")
        refs.append({"reference": token, "source_id": sid, "kind": kind if separator else None})
    return refs


def citation_ids(text: str) -> list[str]:
    return sorted({ref["source_id"] for ref in citation_refs(text)})


def typed_refs(text: str) -> list[str]:
    return sorted({ref["reference"] for ref in citation_refs(text) if ref["kind"] is not None})


def evidence_diagnostics(answer: str, sources: list[dict], pages: list[dict]) -> dict:
    """Check explicit reference levels against actually saved, nonempty evidence only."""
    available = {
        (s["id"], kind)
        for kind, items in (("snippet", sources), ("page", pages))
        for s in items
        if isinstance(s.get("content"), str) and s["content"].strip()
    }
    refs = {ref["reference"]: ref for ref in citation_refs(answer)}
    untyped, invalid, unavailable = [], [], []
    for token, ref in refs.items():
        if ref["kind"] is None:
            untyped.append(token)
        elif not re.fullmatch(r"SRC-[0-9A-F]{8}@(snippet|page)", token):
            invalid.append(token)
        elif (ref["source_id"], ref["kind"]) not in available:
            unavailable.append(token)
    return {
        "contract_version": 1,
        "references": sorted(refs),
        "untyped_references": sorted(untyped),
        "invalid_references": sorted(invalid),
        "unavailable_references": sorted(unavailable),
        "contract_valid": bool(refs) and not (untyped or invalid or unavailable),
        "note": "Evidence-level availability only. Does not check claim support, conflicts, freshness, or uncited facts.",
    }


def supported_reference_errors(refs: list[str], matched_pairs: set[tuple[str, str]]) -> list[str]:
    """Every selected typed support citation needs a quote from that exact saved level."""
    return [
        "support_evidence_level_mismatch"
        for ref in refs
        if tuple(ref.split("@", 1)) not in matched_pairs
    ]
