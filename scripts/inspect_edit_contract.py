"""Offline review units and draft formats; structural checks never score meaning."""

import argparse
import html
import importlib.metadata
import json
import re
from pathlib import Path

if __package__:
    from . import check_minimal_correction as check
else:
    import check_minimal_correction as check

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "eval/edit_contract_v019.json"
FIELDS = {
    "code_visibility": "两种已保存代码的身份",
    "cancellation_trigger": "取消差异的触发条件",
    "protected_task": "被保护任务的状态",
    "caller_await": "调用方与await的状态",
    "scope": "只核验保存片段的范围",
    "citation": "保存正文的引用层次",
}
UNITS = {
    "replacement_only": "只复核替换段是否明确完成局部任务，不能借全文代填细节。",
    "reconstructed_answer": "复核程序代回的全文；目标外保留不认证原稿事实，也不能把局部缺细节说成全文遗失。",
}


def load_fixture(path, root=ROOT):
    raw = path.read_bytes()
    fixture = json.loads(raw)
    check.require(
        type(fixture.get("schema_version")) is int
        and fixture["schema_version"] == 1
        and fixture.get("kind") == "offline_local_edit_contract_development"
        and fixture.get("author", {}).get("kind") == "assistant",
        "contract_fixture_schema_mismatch",
    )
    check.require(
        fixture["base_fixture_file"] == "eval/minimal_correction_v016.json",
        "unexpected_base_fixture",
    )
    base = root / fixture["base_fixture_file"]
    check.require(
        check.sha(base.read_bytes()) == fixture["base_fixture_sha256"], "base_hash_mismatch"
    )
    context = check.load_context(base, root)
    bound, contents = {}, {}
    check.require(
        set(fixture["source_binding"]) == {"edit", "response", "plan", "assistant_review"},
        "invalid_contract_bindings",
    )
    for role, suffix in (
        ("edit", ".json"),
        ("response", ".txt"),
        ("plan", ".json"),
        ("assistant_review", ".json"),
    ):
        item = fixture["source_binding"][role]
        relative = item["file"]
        check.require(isinstance(relative, str), "invalid_contract_path")
        source = (root / relative).resolve()
        check.require(
            not Path(relative).is_absolute()
            and source.is_relative_to((root / "reports").resolve())
            and source.suffix == suffix,
            "invalid_contract_path",
        )
        content = source.read_bytes()
        check.require(check.sha(content) == item["sha256"], "contract_artifact_hash_mismatch")
        bound[relative], contents[role] = item["sha256"], content
    record, plan, review = (json.loads(contents[k]) for k in ("edit", "plan", "assistant_review"))
    check.require(
        record["kind"] == "one_call_local_edit_result"
        and record["plan"] == plan
        and record["plan_file_sha256"] == check.sha(contents["plan"])
        and plan["fixture_sha256"] == context["fixture_sha256"]
        and plan["bound_files_sha256"] == context["bound_files_sha256"],
        "saved_edit_plan_mismatch",
    )
    # Historical records are read as data. Never load the paid executor's plan,
    # .env or client, and never rewrite its old application/version fingerprints.
    check.require(
        check.text_sha(record["request"]["system"]) == plan["system_sha256"]
        and check.text_sha(record["request"]["user"]) == plan["input_sha256"],
        "saved_request_hash_mismatch",
    )
    text = record["response_text"]
    check.require(
        isinstance(text, str)
        and contents["response"] == text.encode("utf-8")
        and check.text_sha(text) == record["response_text_sha256"],
        "saved_response_mismatch",
    )
    candidate = check.check_edit(
        context, {"id": "model_response", "text": text}, "model_adapter_text"
    )
    check.require(record["candidate_check"] == candidate, "saved_candidate_mismatch")
    check.require(
        record["attempts"] == 1
        and len(record["provider_responses"]) == 1
        and record["searches"] == record["page_reads"] == record["tools_executed"] == 0
        and not record["applied_to_production"],
        "unexpected_saved_execution",
    )
    check.require(
        review["edit_file"] == fixture["source_binding"]["edit"]["file"]
        and review["edit_file_sha256"] == check.sha(contents["edit"])
        and review["response_text_sha256"] == record["response_text_sha256"]
        and review["plan_sha256"] == record["plan_file_sha256"]
        and review["fixture_sha256"] == context["fixture_sha256"]
        and review["reviewer"]["kind"] == "assistant"
        and review["human_gold"] is False
        and review["human_review_complete"] is False
        and review["quality_score"] is None,
        "saved_assistant_review_mismatch",
    )
    drafts, ids = fixture["drafts"], {"saved_model_response"}
    check.require(isinstance(drafts, list) and bool(drafts), "invalid_contract_drafts")
    for draft in drafts:
        check.require(
            set(draft) == {"id", "format", "payload"}
            and isinstance(draft["id"], str)
            and bool(draft["id"].strip())
            and draft["id"] not in ids
            and draft["format"] in ("free_text", "structured_fields"),
            "invalid_or_duplicate_contract_draft",
        )
        ids.add(draft["id"])
    return {
        "fixture": fixture,
        "fixture_sha256": check.sha(raw),
        "context": context,
        "bound_files_sha256": bound | context["bound_files_sha256"],
        "record": record,
        "prior_review": review,
    }


def format_draft(format_name, payload):
    """Check shape only. Even contradictory, nonempty fields can pass."""
    errors = []
    if format_name not in ("free_text", "structured_fields"):
        return {"valid": False, "errors": ["unknown_format"], "replacement": None}
    if format_name == "free_text":
        if not isinstance(payload, str) or not payload.strip():
            errors.append("invalid_free_text")
        return {
            "valid": not errors,
            "errors": errors,
            "replacement": payload if not errors else None,
        }
    if not isinstance(payload, dict):
        return {"valid": False, "errors": ["invalid_fields_object"], "replacement": None}
    errors.extend("missing_field_" + key for key in FIELDS if key not in payload)
    if set(payload) - set(FIELDS):
        errors.append("unexpected_fields")
    errors.extend(
        "invalid_field_" + key
        for key in FIELDS
        if key in payload and (not isinstance(payload[key], str) or not payload[key].strip())
    )
    # Preserve values exactly; join only known fields in a fixed order. No trim,
    # model inference, keyword-based factual judgment or rewrite is performed.
    replacement = "；".join(payload[key] for key in FIELDS) + "；" if not errors else None
    return {"valid": not errors, "errors": errors, "replacement": replacement}


def empty_review():
    return {"reviewer": {"kind": "unassigned", "name": ""}, "assessment": None, "complete": False}


def inspect(loaded):
    record = loaded["record"]
    inputs = [
        {"id": "saved_model_response", "format": "free_text", "payload": record["response_text"]}
    ]
    inputs += loaded["fixture"]["drafts"]
    rows = []
    for draft in inputs:
        shape = format_draft(draft["format"], draft["payload"])
        origin = (
            "saved_model_adapter_text"
            if draft["id"] == "saved_model_response"
            else "assistant_handwritten_draft"
        )
        candidate = None
        units = {
            unit: {"text": None, "text_sha256": None, "semantic_review": empty_review()}
            for unit in UNITS
        }
        if shape["valid"]:
            candidate = check.check_edit(
                loaded["context"], {"id": draft["id"], "text": shape["replacement"]}, origin
            )
            for unit, text in (
                ("replacement_only", candidate["replacement"]),
                ("reconstructed_answer", candidate["reconstructed_answer"]),
            ):
                units[unit].update(text=text, text_sha256=check.text_sha(text))
        rows.append(
            {
                "id": draft["id"],
                "origin": origin,
                "format": draft["format"],
                "draft_payload": draft["payload"],
                "format_check": shape,
                "draft_json_characters": len(
                    json.dumps(draft["payload"], ensure_ascii=False, separators=(",", ":"))
                ),
                "candidate_check": candidate,
                "mechanically_valid": bool(candidate and candidate["mechanically_valid"]),
                "review_units": units,
            }
        )
    return {
        "schema_version": 1,
        "kind": "offline_edit_contract_inspection",
        "application_version": importlib.metadata.version("agentscope-deep-researcher-interview"),
        "fixture_sha256": loaded["fixture_sha256"],
        "inspector_sha256": check.sha(Path(__file__).read_bytes()),
        "checker_sha256": check.sha((ROOT / "scripts/check_minimal_correction.py").read_bytes()),
        "bound_files_sha256": loaded["bound_files_sha256"],
        "fixed_evidence": loaded["context"]["fixture"]["fixed_evidence"],
        "original_target": loaded["context"]["fixture"]["edit_task"]["target"],
        "success_conditions": loaded["context"]["fixture"]["success_conditions"],
        "review_unit_definitions": UNITS,
        "structured_field_definitions": FIELDS,
        "prior_model_record": {
            "application_version": record["application_version"],
            "attempts": record["attempts"],
            "token_usage": record["token_usage"],
        },
        "prior_assistant_review": {
            "assessment": loaded["prior_review"]["assistant_assessment"],
            "scope": "replacement_only",
            "human_gold": False,
        },
        "rows": rows,
        "model_calls": 0,
        "searches": 0,
        "page_reads": 0,
        "semantic_score": None,
        "human_review_complete": False,
        "note": "Read-only comparison on one used development source. Field presence and character counts are not semantic scores or token/cost estimates. Only saved_model_response is a historical model output; other rows are assistant drafts. Separate unit reviews remain unassigned. Exit 0 means inspection completed, not that drafts passed or facts are correct.",
    }


def render_inspection(result):
    escape = html.escape
    blocks = []
    for row in result["rows"]:
        candidate = row["candidate_check"] or {}
        unit_blocks = "".join(
            f"<details><summary>{escape(unit)} — 语义待复核</summary><pre>{escape(data['text'] or '格式失败，未生成')}</pre></details>"
            for unit, data in row["review_units"].items()
        )
        blocks.append(
            f"<section><h3>{escape(row['id'])}</h3><p>来源：{escape(row['origin'])}；格式：{escape(row['format'])}；字段/格式合规：{row['format_check']['valid']}；机械合规：{row['mechanically_valid']}</p>"
            f"<p>替换字符：{candidate.get('replacement_chars', '无')}；全文字符：{candidate.get('answer_chars', '无')}；草稿JSON字符：{row['draft_json_characters']}（不是Token）</p>"
            f"<pre>{escape(json.dumps(row['draft_payload'], ensure_ascii=False, indent=2))}</pre>"
            f"<p>{escape(', '.join(row['format_check']['errors'] + candidate.get('errors', [])))}</p>{unit_blocks}</section>"
        )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>离线编辑复核单位与输出约定</title>'
        "<style>body{max-width:1050px;margin:28px auto;padding:0 20px;font:16px/1.6 system-ui}section{border-top:1px solid #bbb;margin-top:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:12px}summary{cursor:pointer}</style>"
        "</head><body><h1>离线编辑复核单位与输出约定</h1><p>本次0模型/0搜索/0读页。只有第一行是旧模型原响应，其他均为助手手写草稿。字段存在、格式和预算通过不代表事实正确；没有质量分数。</p>"
        f"<pre>{escape(json.dumps(result['review_unit_definitions'], ensure_ascii=False, indent=2))}</pre>"
        f"<h2>固定保存证据</h2><pre>{escape(result['fixed_evidence']['page_quote']['text'])}</pre>"
        f"<details><summary>范围元数据与局部任务</summary><pre>{escape(json.dumps({'scope_metadata': result['fixed_evidence']['scope_metadata'], 'conditions': result['success_conditions']}, ensure_ascii=False, indent=2))}</pre></details>"
        + "".join(blocks)
        + f"<details><summary>完整离线记录</summary><pre>{escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre></details></body></html>"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--output", type=Path, help="Explicit new HTML; never overwrites an existing file."
    )
    args = parser.parse_args(argv)
    try:
        result = inspect(load_fixture(args.fixture))
        if args.output:
            with args.output.open("x", encoding="utf-8", newline="\n") as output:
                output.write(render_inspection(result))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, IndexError, OSError, AttributeError) as exc:
        code = (
            str(exc)
            if isinstance(exc, ValueError) and re.fullmatch(r"[a-z_]+", str(exc))
            else "invalid_contract_input_or_output"
        )
        parser.error(code)


if __name__ == "__main__":
    raise SystemExit(main())
