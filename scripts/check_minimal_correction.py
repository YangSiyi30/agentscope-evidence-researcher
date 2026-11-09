"""Inspect a saved local edit offline. Structural validity is not semantic correctness."""

import argparse
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from as_researcher.evidence import citation_refs, quote_evidence_text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "eval/minimal_correction_v016.json"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def text_sha(text):
    return sha(text.encode("utf-8"))


def characters(text):
    return sum(not char.isspace() for char in text)


def require(condition, code):
    if not condition:
        raise ValueError(code)


def refs(text):
    parsed = citation_refs(text)
    # An unclosed extra SRC marker must not disappear from the check.
    complete = len(re.findall(r"\[\s*SRC-", text, re.IGNORECASE)) == len(parsed)
    return sorted({item["reference"] for item in parsed}), complete


def load_context(path, root=ROOT):
    raw = path.read_bytes()
    fixture = json.loads(raw)
    require(
        type(fixture.get("schema_version")) is int
        and fixture["schema_version"] == 1
        and fixture.get("kind") == "offline_minimal_correction_development_fixture"
        and fixture.get("author", {}).get("kind") == "assistant",
        "fixture_schema_mismatch",
    )
    binding = fixture["source_binding"]
    files = {}
    contents = {}
    for key, suffix in (
        ("revision_file", ".json"),
        ("answer_file", ".md"),
        ("source_run_file", ".json"),
        ("historical_run_file", ".json"),
    ):
        relative = binding[key]
        require(isinstance(relative, str), "invalid_bound_path")
        source = (root / relative).resolve()
        require(
            not Path(relative).is_absolute()
            and source.is_relative_to((root / "reports").resolve())
            and source.suffix == suffix,
            "invalid_bound_path",
        )
        content = source.read_bytes()
        require(sha(content) == binding[key + "_sha256"], key + "_fingerprint_mismatch")
        files[relative] = sha(content)
        contents[key] = content
    require(
        contents["source_run_file"] == contents["historical_run_file"],
        "historical_source_copy_mismatch",
    )
    revision = json.loads(contents["revision_file"])
    run = json.loads(contents["source_run_file"])
    require(binding["answer_field"] == "candidate_answer", "unsupported_answer_field")
    answer = revision["candidate_answer"]
    require(
        isinstance(answer, str)
        and text_sha(answer) == binding["answer_text_sha256"] == revision["candidate_sha256"]
        and characters(answer) == binding["answer_nonwhitespace_characters"],
        "answer_text_mismatch",
    )
    require(
        revision["original_answer"] == run["answer"]
        and text_sha(run["answer"]) == binding["original_before_revision_answer_sha256"],
        "original_answer_mismatch",
    )
    # The saved Markdown uses CRLF; hash the raw bytes independently, normalize
    # only that representation for text comparison. Never strip answer content.
    markdown = contents["answer_file"].decode("utf-8")
    require(markdown.replace("\r\n", "\n") == answer, "answer_markdown_text_mismatch")
    task = fixture["edit_task"]
    target = task["target"]
    start, end, text = target["start"], target["end"], target["text"]
    require(
        target["offset_basis"] == "candidate_answer_unicode_codepoints_end_exclusive"
        and type(start) is int
        and type(end) is int
        and 0 <= start < end <= len(answer)
        and isinstance(text, str)
        and answer[start:end] == text
        and text_sha(text) == target["text_sha256"]
        and type(target["occurrences"]) is int
        and target["occurrences"] == answer.count(text) == 1,
        "target_span_mismatch",
    )
    allowed = task["allowed_citation_refs"]
    require(
        isinstance(allowed, list)
        and allowed
        and all(isinstance(ref, str) for ref in allowed)
        and len(set(allowed)) == len(allowed)
        and refs(text) == (sorted(allowed), True),
        "target_citation_mismatch",
    )
    for key in (
        "replacement_max_nonwhitespace_characters",
        "reconstructed_answer_max_nonwhitespace_characters",
    ):
        require(type(task[key]) is int and 0 < task[key] <= 12000, "invalid_edit_budget")
    evidence = fixture["fixed_evidence"]
    quote = evidence["page_quote"]
    require(
        quote["kind"] == "page"
        and quote["field"] == "content"
        and sorted(allowed) == [quote["source_id"] + "@page"]
        and quote_evidence_text(run, quote) == quote["text"]
        and text_sha(quote["text"]) == quote["text_sha256"],
        "fixed_quote_mismatch",
    )
    location = re.fullmatch(r"page_evidence\[(\d+)\]\.segments\[(\d+)\]\.text", quote["location"])
    require(location is not None, "invalid_evidence_location")
    page_index, segment_index = map(int, location.groups())
    page = run["page_evidence"][page_index]
    segment = page["segments"][segment_index]
    require(
        page["id"] == quote["source_id"]
        and segment["segment_id"] == quote["segment_id"]
        and segment["text"] == quote["text"]
        and text_sha(page["content"]) == quote["saved_page_content_sha256"],
        "saved_evidence_mismatch",
    )
    metadata = evidence["scope_metadata"]
    actual = {
        "is_full_document": page["is_full_document"],
        "segment_is_truncated": segment["is_truncated"],
        "segment_code_block_count": segment["code_block_count"],
        "omitted_code_blocks": page["omitted_code_blocks"],
        "omitted_code_contexts": page["omitted_code_contexts"],
    }
    require(
        metadata["location"] == f"page_evidence[{page_index}]"
        and all(
            type(metadata[k]) is type(value) and metadata[k] == value for k, value in actual.items()
        ),
        "scope_metadata_mismatch",
    )
    conditions = fixture["success_conditions"]
    require(
        isinstance(conditions, list)
        and conditions
        and all(isinstance(item, str) and item.strip() for item in conditions),
        "invalid_success_conditions",
    )
    candidates = fixture["handwritten_candidates"]
    require(isinstance(candidates, list) and candidates, "invalid_candidates")
    ids = set()
    for candidate in candidates:
        require(
            isinstance(candidate.get("id"), str)
            and candidate["id"].strip()
            and candidate["id"] not in ids
            and isinstance(candidate.get("text"), str)
            and candidate.get("expected_development_assessment") in ("pass", "fail"),
            "invalid_or_duplicate_candidate",
        )
        ids.add(candidate["id"])
    return {
        "fixture": fixture,
        "fixture_sha256": sha(raw),
        "answer": answer,
        "bound_files_sha256": files,
        "markdown_text_basis": "Raw-byte SHA256 checked; CRLF normalized to LF only for text comparison, with no stripping.",
    }


def check_edit(context, candidate, origin="assistant_handwritten_development_fixture"):
    replacement = candidate["text"]
    require(isinstance(replacement, str), "invalid_replacement_text")
    answer = context["answer"]
    task = context["fixture"]["edit_task"]
    start, end = task["target"]["start"], task["target"]["end"]
    prefix, suffix = answer[:start], answer[end:]
    canonical = prefix + replacement + suffix
    submitted = candidate.get("reconstructed_answer", canonical)
    require(isinstance(submitted, str), "invalid_reconstructed_answer")
    references, complete = refs(replacement)
    outside_unchanged = (
        len(submitted) >= len(prefix) + len(suffix)
        and submitted[: len(prefix)] == prefix
        and (not suffix or submitted[-len(suffix) :] == suffix)
    )
    checks = {
        "replacement_nonempty": bool(replacement.strip()),
        "replacement_within_budget": characters(replacement)
        <= task["replacement_max_nonwhitespace_characters"],
        "answer_within_budget": characters(submitted)
        <= task["reconstructed_answer_max_nonwhitespace_characters"],
        "citation_refs_and_levels_preserved": complete
        and references == sorted(task["allowed_citation_refs"]),
        "outside_target_unchanged": outside_unchanged,
        "answer_matches_exact_replacement": submitted == canonical,
    }
    return {
        "id": candidate["id"],
        "origin": origin,
        "replacement": replacement,
        "replacement_chars": characters(replacement),
        "reconstructed_answer": submitted,
        "answer_chars": characters(submitted),
        "canonical_reconstruction": canonical,
        "reconstruction_mode": "provided_answer_checked"
        if "reconstructed_answer" in candidate
        else "generated_from_fixed_prefix_and_suffix",
        "checks": checks,
        "mechanically_valid": all(checks.values()),
        "errors": [key for key, passed in checks.items() if not passed],
        "development_expectation": {
            "assessment": candidate.get("expected_development_assessment"),
            "reason": candidate.get("reason"),
            "failure_types": candidate.get("failure_types", []),
            "note": "Copied assistant development expectation, not a computed semantic result.",
        },
        "semantic_review": {
            "reviewer": {"kind": "unassigned", "name": ""},
            "assessment": None,
            "complete": False,
        },
    }


def inspect(
    context,
    candidates=None,
    origin="assistant_handwritten_development_fixture",
    candidate_file_sha256=None,
):
    fixture = context["fixture"]
    rows = [
        check_edit(context, candidate, origin)
        for candidate in (
            candidates if candidates is not None else fixture["handwritten_candidates"]
        )
    ]
    return {
        "schema_version": 1,
        "kind": "offline_minimal_edit_inspection",
        "created_at": datetime.now(UTC).isoformat(),
        "fixture_valid": True,
        "fixture_sha256": context["fixture_sha256"],
        "checker_sha256": sha(Path(__file__).read_bytes()),
        "evidence_helper_sha256": sha((ROOT / "src/as_researcher/evidence.py").read_bytes()),
        "bound_files_sha256": context["bound_files_sha256"],
        "candidate_file_sha256": candidate_file_sha256,
        "markdown_text_basis": context["markdown_text_basis"],
        "task": fixture["edit_task"],
        "fixed_evidence": fixture["fixed_evidence"],
        "success_conditions_for_review": fixture["success_conditions"],
        "original_answer": context["answer"],
        "candidates": rows,
        "all_candidates_mechanically_valid": all(row["mechanically_valid"] for row in rows),
        "model_calls": 0,
        "searches": 0,
        "page_reads": 0,
        "semantic_score": None,
        "human_review_complete": False,
        "note": "Read-only saved-evidence inspection. Expected labels are handwritten assistant metadata; structural passes do not certify semantics, other answer text, human review or model quality. External candidate origin is unassigned. This command makes no model/network calls and does not load .env.",
    }


def render_inspection(result):
    def escape(value):
        return html.escape(str(value))

    blocks = []
    for row in result["candidates"]:
        checks = "".join(
            f"<li>{escape(key)}: {passed}</li>" for key, passed in row["checks"].items()
        )
        blocks.append(
            f"<section><h2>{escape(row['id'])}</h2>"
            f"<p>机械检查：{row['mechanically_valid']}；语义复核：待分配。"
            f"替换段 {row['replacement_chars']} 字符，重建答案 {row['answer_chars']} 字符。</p>"
            f"<p>来源：{escape(row['origin'])}；重建方式：{escape(row['reconstruction_mode'])}</p>"
            f"<details><summary>助手预定义预期（不是检查器结论）</summary><pre>{escape(json.dumps(row['development_expectation'], ensure_ascii=False, indent=2))}</pre></details>"
            f"<h3>替换段</h3><pre>{escape(row['replacement'])}</pre>"
            f"<h3>重建答案</h3><pre>{escape(row['reconstructed_answer'])}</pre>"
            f"<details><summary>程序按固定前后文重建的文本</summary><pre>{escape(row['canonical_reconstruction'])}</pre></details>"
            f"<ul>{checks}</ul></section>"
        )
    conditions = "".join(
        f"<li>{escape(item)}</li>" for item in result["success_conditions_for_review"]
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>单句纠错：离线机械检查与待复核材料</title>"
        "<style>body{max-width:1000px;margin:32px auto;padding:0 20px;font:16px/1.6 system-ui;color:#202830}section{border-top:1px solid #bbb;margin-top:28px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:14px}summary{cursor:pointer}</style>"
        "</head><body><h1>单句纠错：离线机械检查与待复核材料</h1>"
        "<p>0 模型调用、0 搜索、0 读页。语义结论未填写，没有质量评分。</p>"
        "<p>默认候选是助手手写开发材料。机械合规不能证明代码解释、取消条件或版本正确；目标外原文保持也不认证整篇答案。</p>"
        f"<h2>固定证据正文</h2><pre>{escape(result['fixed_evidence']['page_quote']['text'])}</pre>"
        f"<h3>范围元数据（与正文分开）</h3><pre>{escape(json.dumps(result['fixed_evidence']['scope_metadata'], ensure_ascii=False, indent=2))}</pre>"
        f"<h2>待逐句复核的条件</h2><ul>{conditions}</ul>"
        f"<h2>原目标短句</h2><pre>{escape(result['task']['target']['text'])}</pre>"
        + "".join(blocks)
        + f"<details><summary>完整机械记录及指纹</summary><pre>{escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre></details>"
        "</body></html>"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--candidate", help="One handwritten candidate ID; default inspects all."
    )
    selection.add_argument(
        "--candidate-file",
        type=Path,
        help="Offline JSON: schema_version=1, id, text, optional reconstructed_answer.",
    )
    parser.add_argument(
        "--output", type=Path, help="Explicit new HTML file; existing files are never replaced."
    )
    args = parser.parse_args(argv)
    try:
        context = load_context(args.fixture)
        candidates, origin, external_hash = None, "assistant_handwritten_development_fixture", None
        if args.candidate:
            candidates = [
                item
                for item in context["fixture"]["handwritten_candidates"]
                if item["id"] == args.candidate
            ]
            require(bool(candidates), "unknown_candidate_id")
        elif args.candidate_file:
            raw = args.candidate_file.read_bytes()
            candidate = json.loads(raw)
            require(
                isinstance(candidate, dict)
                and type(candidate.get("schema_version")) is int
                and candidate["schema_version"] == 1
                and set(candidate)
                in (
                    {"schema_version", "id", "text"},
                    {"schema_version", "id", "text", "reconstructed_answer"},
                )
                and isinstance(candidate.get("id"), str)
                and candidate["id"].strip(),
                "invalid_external_candidate",
            )
            candidates, origin, external_hash = (
                [candidate],
                "unassigned_offline_candidate_file",
                sha(raw),
            )
        result = inspect(context, candidates, origin, external_hash)
        if args.output:
            rendered = render_inspection(result)
            with args.output.open("x", encoding="utf-8", newline="\n") as output:
                output.write(rendered)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["all_candidates_mechanically_valid"] else 1
    except (ValueError, KeyError, TypeError, IndexError, OSError, AttributeError) as exc:
        # Do not echo raw file contents or arbitrary exception messages.
        code = (
            str(exc)
            if isinstance(exc, ValueError) and re.fullmatch(r"[a-z_]+", str(exc))
            else "invalid_input_or_output"
        )
        parser.error(code)


if __name__ == "__main__":
    raise SystemExit(main())
