"""Deterministic HTML blocks and lexical passage selection; no model or vector DB."""

import hashlib
import math
import re
from collections import Counter
from html.parser import HTMLParser

BLOCKS = {"p", "li", "pre", "dd", "dt", "td", "th", "div", "section", "article"}
HEADINGS = {f"h{i}" for i in range(1, 7)}
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
SKIP = {"script", "style", "noscript", "nav", "footer", "header", "iframe", "svg", "template"}


class TableRows:
    """Only assign columns for rectangular tables with one explicit header row.

    Complex layouts retain ordered text, explicitly without inferred headers.
    No browser layout engine or additional HTML dependency is needed.
    """

    def __init__(self):
        self.depth = 1
        self.rows, self.cells, self.parts = [], [], []
        self.cell_tag = None
        self.complex_layout = False
        self.hidden = []

    def end_cell(self):
        if self.cell_tag:
            self.cells.append({"tag": self.cell_tag, "text": " ".join("".join(self.parts).split())})
        self.cell_tag, self.parts = None, []

    def end_row(self):
        self.end_cell()
        if self.cells:
            self.rows.append(self.cells)
        self.cells = []

    def start(self, tag, attrs):
        attrs = dict(attrs)
        if self.hidden:
            if tag not in VOID:
                self.hidden.append(tag)
            return
        if tag in SKIP or "hidden" in attrs or attrs.get("aria-hidden") == "true":
            if tag not in VOID:
                self.hidden.append(tag)
            return
        if tag == "table":
            self.depth += 1
            self.complex_layout = True
        elif tag == "tr":
            self.end_row()
        elif tag in {"td", "th"}:
            self.end_cell()
            self.cell_tag = tag
            if attrs.get("rowspan", "1") != "1" or attrs.get("colspan", "1") != "1":
                self.complex_layout = True
        elif tag in BLOCKS | {"br"}:
            self.parts.append(" ")

    def end(self, tag):
        if self.hidden:
            if tag in self.hidden:
                self.hidden = self.hidden[: len(self.hidden) - 1 - self.hidden[::-1].index(tag)]
            return
        if tag == "table":
            self.depth -= 1
            self.end_row()
        elif tag == "tr":
            self.end_row()
        elif tag in {"td", "th"}:
            self.end_cell()
        elif tag in BLOCKS:
            self.parts.append(" ")

    def data(self, data):
        if not self.hidden and self.cell_tag:
            self.parts.append(data)

    def normalized_rows(self):
        self.end_row()
        headers = []
        if self.rows and all(c["tag"] == "th" and c["text"] for c in self.rows[0]):
            headers = [c["text"] for c in self.rows[0]]
        if any(all(c["tag"] == "th" for c in row) for row in self.rows[1:]):
            self.complex_layout = True
        for index, row in enumerate(self.rows):
            mapped = (
                bool(headers) and not self.complex_layout and len(row) == len(headers) and index > 0
            )
            body = (
                " | ".join(
                    f"{header}: {cell['text']}" for header, cell in zip(headers, row, strict=True)
                )
                if mapped
                else " | ".join(c["text"] for c in row)
            )
            if body.strip(" |"):
                yield {
                    "text": body,
                    "kind": "table_row" if mapped else "table_unmapped",
                    "table_mapping": "header_value" if mapped else "unavailable",
                    "table_row_index": index,
                }


class BlockParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.rows = []
        self.headings = {}
        self.kind = "p"
        self.link_chars = 0
        self.definitions = []
        self.definition_ids = []
        self.next_definition_id = 0
        self.table = None

    def context(self):
        return {
            "heading_path": list(self.headings.values()) + [d for d in self.definitions if d],
            "definition_path": [d for d in self.definitions if d],
            "definition_id": self.definition_ids[-1] if self.definition_ids else None,
        }

    def end_table(self):
        for row in self.table.normalized_rows():
            self.rows.append({**row, **self.context(), "link_density": 0.0})
        self.table = None
        self.kind = "p"

    def flush(self):
        raw = "".join(self.parts)
        text = (
            raw.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            if self.kind == "pre"
            else re.sub(r"\s+", " ", raw).strip().strip("\u200b¶ ")
        )
        if text.strip():
            if self.kind in HEADINGS:
                level = int(self.kind[1])
                self.headings = {k: v for k, v in self.headings.items() if k < level}
                self.headings[level] = text
            if self.kind == "dt" and self.definitions:
                self.definitions[-1] = text
                self.next_definition_id += 1
                self.definition_ids[-1] = self.next_definition_id
            self.rows.append(
                {
                    "text": text,
                    "kind": self.kind,
                    **self.context(),
                    "link_density": self.link_chars / max(1, len(raw)),
                }
            )
        self.parts, self.link_chars = [], 0

    def handle_starttag(self, tag, attrs):
        if self.table is not None:
            self.table.start(tag, attrs)
            return
        attrs = dict(attrs)
        inherited = bool(self.stack and self.stack[-1][1])
        hint = " ".join(str(attrs.get(k, "")) for k in ("class", "id"))
        skip = (
            inherited
            or tag in SKIP
            or "hidden" in attrs
            or attrs.get("aria-hidden") == "true"
            or attrs.get("role") == "navigation"
            or bool(
                set(hint.lower().split())
                & {"toc", "sidebar", "breadcrumb", "breadcrumbs", "table-of-contents"}
            )
        )
        if tag == "table" and not skip:
            self.flush()
            self.table = TableRows()
            return
        in_heading = any(t in HEADINGS | {"dt"} for t, _ in self.stack)
        in_pre = any(t == "pre" for t, _ in self.stack)
        if not inherited and tag in BLOCKS | HEADINGS and not in_heading and not in_pre:
            self.flush()
            self.kind = tag if tag in HEADINGS else "p" if tag not in BLOCKS else tag
        if tag not in VOID:
            self.stack.append((tag, skip))
            if tag == "dl" and not skip:
                self.flush()
                self.definitions.append(None)
                self.definition_ids.append(None)
        elif tag == "br" and not skip:
            self.parts.append("\n" if in_pre else " ")

    def handle_endtag(self, tag):
        if self.table is not None:
            self.table.end(tag)
            if self.table.depth == 0:
                self.end_table()
            return
        position = next(
            (i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None
        )
        if position is None:
            return
        in_heading = any(t in HEADINGS | {"dt"} for t, _ in self.stack)
        in_pre = any(t == "pre" for t, _ in self.stack)
        if (
            not self.stack[-1][1]
            and tag in BLOCKS | HEADINGS
            and (tag in HEADINGS | {"dt"} or not in_heading)
            and (tag == "pre" or not in_pre)
        ):
            self.flush()
            self.kind = "p"
        if tag == "dl" and not self.stack[-1][1] and self.definitions:
            self.flush()
            self.definitions.pop()
            self.definition_ids.pop()
        self.stack = self.stack[:position]

    def handle_data(self, data):
        if self.table is not None:
            self.table.data(data)
            return
        if self.stack and self.stack[-1][1]:
            return
        self.parts.append(data)
        if any(tag == "a" for tag, _ in self.stack):
            self.link_chars += len(data)


def bind_code_context(rows: list[dict]) -> list[dict]:
    """Keep short introductions, code and a following qualification together.

    Structural heuristic within one innermost definition and heading only.
    Short colon-ended bridges can connect successive code examples. No code
    execution, language parsing, or semantic equivalence is inferred.
    """

    def same_scope(a, b):
        return (
            a["definition_id"] is not None
            and a["definition_id"] == b["definition_id"]
            and a["heading_path"] == b["heading_path"]
        )

    def intro(row):
        return (
            row["kind"] in {"p", "dd", "div"}
            and len(row["text"]) <= 160
            and row["text"].endswith((":", "："))
        )

    grouped, index = [], 0
    while index < len(rows):
        anchor = rows[index]
        if not (
            intro(anchor)
            and index + 1 < len(rows)
            and rows[index + 1]["kind"] == "pre"
            and same_scope(anchor, rows[index + 1])
        ):
            grouped.append(anchor)
            index += 1
            continue
        end = index + 2
        while (
            end + 1 < len(rows)
            and intro(rows[end])
            and rows[end + 1]["kind"] == "pre"
            and same_scope(anchor, rows[end])
            and same_scope(anchor, rows[end + 1])
        ):
            end += 2
        if (
            end < len(rows)
            and rows[end]["kind"] in {"p", "dd", "div"}
            and not intro(rows[end])
            and len(rows[end]["text"]) <= 500
            and same_scope(anchor, rows[end])
        ):
            end += 1
        members, offset = [], 0
        for row in rows[index:end]:
            members.append({"kind": row["kind"], "start": offset, "end": offset + len(row["text"])})
            offset += len(row["text"]) + 2
        grouped.append(
            {
                **anchor,
                "kind": "code_context",
                "text": "\n\n".join(row["text"] for row in rows[index:end]),
                "members": members,
                "member_offset_basis": "segment_text_unicode_codepoints_end_exclusive",
                "code_block_count": sum(row["kind"] == "pre" for row in rows[index:end]),
                "link_density": max(row["link_density"] for row in rows[index:end]),
            }
        )
        index = end
    return grouped


def parse_document(html: str) -> dict:
    parser = BlockParser()
    parser.feed(html)
    if parser.table is not None:
        parser.end_table()
    parser.flush()
    rows = []
    seen_definitions = set()
    for row in bind_code_context(parser.rows):
        if row["definition_id"] is not None and row["kind"] in {"p", "dd", "div"}:
            row = {
                **row,
                "kind": "definition",
                "definition_lead": row["definition_id"] not in seen_definitions,
            }
            seen_definitions.add(row["definition_id"])
            if (
                rows
                and rows[-1]["kind"] == "definition"
                and rows[-1]["definition_id"] == row["definition_id"]
                and rows[-1]["heading_path"] == row["heading_path"]
            ):
                rows[-1]["text"] += "\n\n" + row["text"]
                rows[-1]["link_density"] = max(rows[-1]["link_density"], row["link_density"])
                continue
        rows.append(row)
    text = "\n\n".join(row["text"] for row in rows)
    digest = hashlib.sha256(text.encode()).hexdigest()
    passages, offset = [], 0
    for row in rows:
        # Code and tables stay whole. Never emit a partial code example as complete evidence.
        atomic = row["kind"].startswith("table_") or row["kind"] in {"pre", "code_context"}
        width = len(row["text"]) if atomic else 900
        for local in range(0, len(row["text"]), width):
            body = row["text"][local : local + width]
            start = offset + local
            identity = f"{digest}:{start}:{start + len(body)}"
            passages.append(
                {
                    **row,
                    "definition_lead": row.get("definition_lead", False) and local == 0,
                    "text": body,
                    "start": start,
                    "end": start + len(body),
                    "segment_id": "SEG-" + hashlib.sha256(identity.encode()).hexdigest()[:12],
                }
            )
        offset += len(row["text"]) + 2
    return {"text": text, "document_sha256": digest, "passages": passages}


def retrieval_tokens(text: str) -> list[str]:
    """Lexical units, NOT translation/embeddings: identifiers and CJK bigrams.

    Keep full Python identifiers (wait_for / asyncio.timeout) and their parts.
    Chinese bigrams make partial Chinese wording match without a new tokenizer.
    """
    stop = {
        "a",
        "an",
        "the",
        "is",
        "are",
        "of",
        "to",
        "in",
        "on",
        "and",
        "or",
        "for",
        "with",
        "what",
        "when",
        "why",
        "how",
        "does",
        "do",
        "it",
        "its",
        "be",
        "by",
        "this",
        "that",
        "from",
        "please",
        "explain",
        "according",
        "which",
        "as",
        "at",
    }
    tokens = []
    for match in re.finditer(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*|[\u3400-\u9fff]+", text.casefold()):
        word = match.group()
        if "\u3400" <= word[0] <= "\u9fff":
            tokens.extend(word[i : i + 2] for i in range(max(1, len(word) - 1)))
        else:
            if word not in stop:
                tokens.append(word)
            if "." in word or "_" in word:
                parts = list(dict.fromkeys(word.split(".") + re.split(r"[._]+", word)))
                tokens.extend(t for t in parts if t and t != word and t not in stop)
    return tokens


def bm25_scores(passages: list[dict], focus: str) -> list[float]:
    """Rank passages within ONE page; k1=1.2, b=.75, heading tokens weighted 2.

    Positive log(1 + ...) IDF avoids negative scores on small page corpora.
    Query frequencies are ignored; repeating a word cannot increase its weight.
    This is a local lexical heuristic, not a factual-support score.
    """
    query = list(dict.fromkeys(retrieval_tokens(focus)))[:128]
    bags = [Counter(retrieval_tokens(p["text"])) for p in passages]
    headings = [Counter(retrieval_tokens(" / ".join(p["heading_path"]))) for p in passages]
    lengths = [sum(bag.values()) for bag in bags]
    mean = sum(lengths) / max(1, len(lengths)) or 1
    df = {term: sum(term in b or term in h for b, h in zip(bags, headings)) for term in query}
    scores = []
    for bag, heading, length in zip(bags, headings, lengths):
        score = 0.0
        for term in query:
            tf = bag[term] + 2 * heading[term]
            if tf:
                idf = math.log1p((len(bags) - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / mean))
        scores.append(score)
    return scores


def select_passages(
    document: dict,
    focus: str,
    limit: int,
    *,
    ranking: str = "lexical",
    compact_prefix: bool = False,
) -> dict:
    if ranking == "balanced":
        return select_balanced(document, focus, limit)
    if ranking not in {"lexical", "bm25"}:
        raise ValueError("Unknown passage ranking")
    terms = [x.strip().casefold() for x in re.split(r"[,，;；]", focus) if x.strip()][:3]
    bm_scores = bm25_scores(document["passages"], focus) if ranking == "bm25" else None
    identifiers = {t for t in retrieval_tokens(focus) if "_" in t or "." in t}
    ranked = []
    for index, passage in enumerate(document["passages"]):
        body = passage["text"].casefold()
        heading = " / ".join(passage["heading_path"]).casefold()
        hits = sum(term in body for term in terms)
        score = 4 * hits + 8 * sum(term in heading for term in terms)
        score += sum(min(2, body.count(term)) for term in terms)
        if passage["kind"] in HEADINGS | {"dt"}:
            score *= 0.2
        if passage["kind"] == "pre":
            score *= 0.4
        if passage["link_density"] > 0.7:
            score *= 0.15
        if hits and passage["kind"] == "table_row":
            score += 4
        if (
            terms
            and (passage.get("definition_lead") or passage["kind"] == "code_context")
            and passage["definition_path"]
            and terms[0] in passage["definition_path"][-1].casefold()
        ):
            score += 12
        # Examples must not crowd out the primary definition's initial conditions.
        # A small budget may omit an entire example instead; record that explicitly.
        primary_definition = bool(
            terms
            and passage.get("definition_lead")
            and passage["definition_path"]
            and terms[0] in passage["definition_path"][-1].casefold()
        )
        if bm_scores is not None:
            score = bm_scores[index]
            # Keep extraction safeguards, but replace literal long-phrase matching.
            if passage["kind"] in HEADINGS | {"dt"}:
                score *= 0.2
            if passage["kind"] == "pre":
                score *= 0.4
            if passage["link_density"] > 0.7:
                score *= 0.15
            primary_definition = primary_definition or bool(
                passage.get("definition_lead")
                and passage["definition_path"]
                and identifiers.intersection(retrieval_tokens(passage["definition_path"][-1]))
            )
        ranked.append(
            {**passage, "score": round(score, 3), "primary_definition": primary_definition}
        )
    # Stable ties; empty/unmatched focus falls back to document order.
    ranked.sort(key=lambda p: (-int(p["primary_definition"]), -p["score"], p["start"]))
    if ranked and ranked[0]["score"] > 0:
        ranked = [p for p in ranked if p["score"] > 0]
    selected, remaining, omitted_table_rows, omitted_code_blocks, omitted_code_contexts = (
        [],
        limit,
        0,
        0,
        0,
    )
    omitted_code = []
    for passage in ranked:
        prefix = f"[{passage['segment_id']}] "
        if passage["heading_path"]:
            heading = (
                passage["heading_path"][-1]
                if compact_prefix
                else " / ".join(passage["heading_path"])
            )
            prefix += heading[-min(100, limit // 5) :] + ": "
        if passage["kind"] == "table_unmapped":
            prefix += "[table columns unmapped] "
        if passage["kind"] == "code_context":
            prefix += "[code + adjacent explanation] "
        needed = len(prefix) + (2 if selected else 0)
        atomic = passage["kind"].startswith("table_") or passage["kind"] in {"pre", "code_context"}
        if remaining <= needed or (atomic and len(passage["text"]) > remaining - needed):
            omitted_table_rows += int(passage["kind"].startswith("table_"))
            omitted_code_blocks += int(passage["kind"] == "pre") + passage.get(
                "code_block_count", 0
            )
            omitted_code_contexts += int(passage["kind"] == "code_context")
            if passage["kind"] in {"pre", "code_context"}:
                omitted_code.append(passage)
            continue
        content = passage["text"][: remaining - needed]
        selected.append(
            {
                **passage,
                "text": content,
                "end": passage["start"] + len(content),
                "display_prefix": prefix,
                "is_truncated": len(content) < len(passage["text"]),
            }
        )
        remaining -= needed + len(content)
    selected.sort(key=lambda p: p["start"])
    content = "\n\n".join(p["display_prefix"] + p["text"] for p in selected)
    return {
        "content": content,
        "strategy": "bm25" if ranking == "bm25" else "segments",
        "selector_version": "bm25-v1" if ranking == "bm25" else "lexical-v5",
        "document_sha256": document["document_sha256"],
        "document_chars": len(document["text"]),
        "segments": selected,
        "offset_basis": "normalized_extracted_text",
        "is_full_document": False,
        "selection_warnings": (
            (["partial_prose_fragment"] if any(p["is_truncated"] for p in selected) else [])
            + (["table_rows_omitted_for_budget"] if omitted_table_rows else [])
            + (["code_blocks_omitted_for_budget"] if omitted_code_blocks else [])
            + (["code_context_omitted_for_budget"] if omitted_code_contexts else [])
            + (
                ["table_column_mapping_unavailable"]
                if any(p["kind"] == "table_unmapped" for p in selected)
                else []
            )
        ),
        "omitted_table_rows": omitted_table_rows,
        "omitted_code_blocks": omitted_code_blocks,
        "omitted_code_contexts": omitted_code_contexts,
        "code_selection": code_selection_summary(selected, omitted_code),
    }


def focus_facets(focus: str) -> list[str]:
    """Explicit comma/semicolon facets only; no guessed decomposition/translation."""
    return list(dict.fromkeys(t.strip() for t in re.split(r"[,，;；]", focus) if t.strip()))[:3]


def select_balanced(document: dict, focus: str, limit: int) -> dict:
    """Reserve a fair first round for up to three explicit focuses, then refill.

    Reuse BM25 and the atomic code/table packer. A reservation guarantees a
    budget opportunity, not semantic coverage: a facet can be absent or require
    an indivisible block larger than its quota. Never silently split that block.
    """
    facets = focus_facets(focus)
    if len(facets) < 2:
        result = select_passages(document, focus, limit, ranking="bm25")
        result.update(strategy="balanced", selector_version="balanced-v2")
        return result
    selected = {}
    used = 0
    # Each independent pack includes prefix overhead. Joining nonempty packs
    # costs two more characters; reserve those before dividing the content.
    quota = max(0, (limit - 2 * (len(facets) - 1)) // len(facets))
    warnings = set()
    diagnostics = []
    omitted = []
    matching_ids = set()
    for facet in facets:
        # Do not count order fallback as a relevant match for a missing facet.
        scores = bm25_scores(document["passages"], facet)
        candidates = [p for p, score in zip(document["passages"], scores) if score > 0]
        matching_ids.update(p["segment_id"] for p in candidates)
        # Explicit API names can use exact innermost definition boundaries.
        # Preserve case: asyncio.timeout and asyncio.Timeout are different APIs.
        scoped = []
        if re.fullmatch(r"[A-Za-z_][\w.]*", facet):
            scoped = [
                p
                for p in candidates
                if p["definition_path"]
                and re.search(
                    r"(?<!\w)" + re.escape(facet) + r"(?![\w.])", p["definition_path"][-1]
                )
            ]
        if scoped:
            candidates = scoped
        result = select_passages(
            {**document, "passages": candidates}, facet, quota, ranking="bm25", compact_prefix=True
        )
        added = 0
        for passage in result["segments"]:
            key = passage["segment_id"]
            if key not in selected:
                selected[key] = passage
                used += (
                    len(passage["display_prefix"])
                    + len(passage["text"])
                    + (2 if len(selected) > 1 else 0)
                )
                added += 1
        diagnostics.append(
            {
                "focus": facet,
                "scope": "exact_definition" if scoped else "page_lexical",
                "reserved_chars": quota,
                "matched_candidates": len(candidates),
                "new_segments": added,
            }
        )
        if not candidates:
            warnings.add("focus_no_lexical_match")
        elif not result["segments"]:
            warnings.add("focus_budget_insufficient")
    # Released duplicate/unused quota is available to remaining complete spans.
    # Already selected prose can be extended, but never create two SEG copies.
    refill = select_passages(document, focus, limit, ranking="bm25", compact_prefix=True)
    for passage in refill["segments"]:
        prior = selected.get(passage["segment_id"])
        if prior:
            extra = (
                len(passage["text"])
                + len(passage["display_prefix"])
                - len(prior["text"])
                - len(prior["display_prefix"])
            )
            if len(passage["text"]) > len(prior["text"]) and 0 < extra <= limit - used:
                selected[passage["segment_id"]] = passage
                used += extra
        else:
            extra = len(passage["display_prefix"]) + len(passage["text"]) + (2 if selected else 0)
            if extra <= limit - used:
                selected[passage["segment_id"]] = passage
                used += extra
    rows = sorted(selected.values(), key=lambda p: p["start"])
    for passage in document["passages"]:
        if passage["kind"] in {"pre", "code_context"} and passage["segment_id"] not in selected:
            if passage["segment_id"] in matching_ids:
                omitted.append(passage)
    if any(p["is_truncated"] for p in rows):
        warnings.add("partial_prose_fragment")
    if omitted:
        warnings.add("code_blocks_omitted_for_budget")
    if any(p["kind"] == "code_context" for p in omitted):
        warnings.add("code_context_omitted_for_budget")
    if any(p["kind"] == "table_unmapped" for p in rows):
        warnings.add("table_column_mapping_unavailable")
    # Table omission is counted for positively matching candidates, just as code.
    missing_tables = sum(
        p["kind"].startswith("table_")
        and p["segment_id"] not in selected
        and p["segment_id"] in matching_ids
        for p in document["passages"]
    )
    if missing_tables:
        warnings.add("table_rows_omitted_for_budget")
    return {
        **refill,
        "strategy": "balanced",
        "selector_version": "balanced-v2",
        "content": "\n\n".join(p["display_prefix"] + p["text"] for p in rows),
        "segments": rows,
        "focus_allocation": {
            "policy": "equal_first_round_then_global_refill",
            "facets": diagnostics,
            "note": "lexical opportunity only; not semantic coverage or full-definition completeness",
        },
        "selection_warnings": sorted(warnings),
        "omitted_table_rows": missing_tables,
        "omitted_code_blocks": sum(
            p.get("code_block_count", int(p["kind"] == "pre")) for p in omitted
        ),
        "omitted_code_contexts": sum(p["kind"] == "code_context" for p in omitted),
        "code_selection": code_selection_summary(rows, omitted),
    }


def code_selection_summary(selected: list[dict], omitted: list[dict]) -> dict:
    """Bound observation overhead; completeness is about extraction, not meaning.

    SEG IDs still resolve against the saved document hash and normalized offsets.
    Only ranked candidates appear here. Absence from a capped detail list says
    nothing about the rest of the page or completeness of a definition.
    """
    retained = [p for p in selected if p["kind"] in {"pre", "code_context"}]

    def detail(passage):
        return {
            "segment_id": passage["segment_id"],
            "heading": " / ".join(passage["heading_path"])[-80:],
            "kind": passage["kind"],
            "code_blocks": passage.get("code_block_count", 1),
        }

    return {
        "scope": "ranked_candidates_only",
        "completeness_basis": "extracted_segment_not_whole_definition_or_page",
        "retained": {
            "state": "complete_extracted_segment",
            "total": len(retained),
            "details": [detail(p) for p in retained[:4]],
            "unlisted": max(0, len(retained) - 4),
        },
        "omitted": {
            "reason": "page_character_budget",
            "total": len(omitted),
            "details": [detail(p) for p in omitted[:4]],
            "unlisted": max(0, len(omitted) - 4),
        },
    }
