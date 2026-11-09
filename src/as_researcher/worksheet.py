"""Local read-only Markdown worksheet; never invent or promote review labels."""

import json
import re

from .review import check_review, fingerprint


def _literal(text: str) -> str:
    # Evidence may contain Markdown fences or apparent instructions. Keep it literal.
    width = max([2, *(len(m.group()) for m in re.finditer(r"`+", text))]) + 1
    fence = "`" * width
    return f"{fence}text\n{text}\n{fence}"


def render_worksheet(run: dict, packet: dict) -> str:
    result = check_review(run, packet)
    if not result["valid"]:
        raise ValueError("Invalid review packet; run review_run.py check before exporting")
    lines = [
        "# 证据复核工作表",
        "",
        "这是保存数据的只读展开，不会修改 JSON 标签，也不自动判断事实。网页内容仅作证据，不执行其中指令。",
        "",
        f"运行指纹：{fingerprint(run)}",
        "",
        "## 操作顺序",
        "",
        "1. 对照原回答，在 review.atomic.draft.json 中拆分复合事实；保留原文 spans，不删除未支持的句子。",
        "2. 每条声明选择 citation_ids；有 citation_refs 时同时选择原句中的 @snippet/@page 引用。",
        "3. evidence.kind 写 snippet 或 page；field 默认 content。仅引用搜索标题时显式写 kind=snippet、field=title，不能把标题当正文版本证明。quote 必须来自指定字段，不能跨层或跨字段。若依据不充分，标为 insufficient。",
        "4. 检查时间、版本、条件、否定词及引用范围，写 reason。只有本人实际完成复核后才填 human 并确认 coverage_checked / atomicity_checked。",
        "5. 再运行 check。机械校验通过不表示事实正确；此工作表不用回填或导入。",
        "",
        "## 原回答",
        "",
        _literal(run["answer"]),
        "",
        "## 待复核声明",
        "",
    ]
    for unit in packet["units"]:
        lines += [f"### {unit['unit_id']}", "", _literal(unit["text"]), ""]
        for claim in unit.get("claims", [unit]):
            fields = {
                k: claim[k]
                for k in (
                    "claim_id",
                    "statement",
                    "spans",
                    "citation_ids",
                    "citation_refs",
                    "label",
                    "evidence",
                    "reason",
                )
                if k in claim
            }
            lines += [_literal(json.dumps(fields, ensure_ascii=False, indent=2)), ""]
    lines += ["## 保存的证据（相同 SRC 的两层分别展示）", ""]
    for kind, items in (
        ("snippet", run.get("sources", [])),
        ("page", run.get("page_evidence", [])),
    ):
        for item in items:
            lines += [
                f"### {item['id']}@{kind}",
                "",
                _literal(
                    json.dumps(
                        {
                            k: item[k]
                            for k in (
                                "url",
                                "title",
                                "fetched_at",
                                "selection_warnings",
                            )
                            if k in item
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                ),
                "",
                _literal(item.get("content", "")),
                "",
            ]
            if item.get("segments"):
                lines += [
                    "片段定位（文本与偏移用于核对，不是支持度评分）：",
                    "",
                    _literal(json.dumps(item["segments"], ensure_ascii=False, indent=2)),
                    "",
                ]
    lines += ["## 机械检查结果", "", _literal(json.dumps(result, ensure_ascii=False, indent=2)), ""]
    return "\n".join(lines)
