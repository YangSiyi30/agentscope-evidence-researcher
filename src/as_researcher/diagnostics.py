"""Read-only, evidence-bound evaluation reports; rules suggest, never judge meaning."""

import csv
import html
import io
import json
from collections import Counter
from pathlib import Path

from .benchmark import assess_saved_batch, check_task_review, digest, source_digest

LAYERS = {
    "L1": "证据获取：搜索/抓页是否成功",
    "L2": "证据选择：原页有而选段未包含",
    "L3": "上下文交付：选中内容是否实际交给模型",
    "L4": "回答生成：覆盖、支持、范围、引用与长度",
    "L5": "评测：题目标准、复核或门槛是否误判",
}
NOTE = (
    "规则信号不是根因标签。L2/L3必须比对冻结原文、保存选段和实际观察，不能仅凭漏答推断。"
    "null表示尚未判定；无效复核的标签不进入矩阵。assistant不等于独立人工gold。"
    "对抗与普通问答分组展示。token不是货币费用，缺失用量不按零处理。"
)


def trial_signals(row: dict, task) -> list[dict]:
    """Return observable symptoms and explicitly tentative investigation routes."""
    assessment = row.get("assessment", {})
    checks = assessment.get("operational", {}).get("checks", {})
    quality = assessment.get("quality", {})
    signals = []
    for gate, value in checks.items():
        if value is not False:
            continue
        candidates = (
            ["L1"]
            if gate in {"saved_evidence", "search_finished_without_error"}
            else ["L4"]
            if gate
            in {
                "answer_within_limit",
                "task_answer_within_limit",
                "references_available",
                "citations_present",
                "permitted_tools",
            }
            else []
        )
        signals.append({"code": "gate:" + gate, "candidate_layers": candidates})
    if quality.get("valid") is False:
        signals.append({"code": "invalid_review", "candidate_layers": ["L5"]})
    else:
        for criterion in task.rubric:
            if criterion.id in quality.get("failed_required", []):
                signals.append(
                    {
                        "code": "criterion:" + criterion.id,
                        "dimension": criterion.dimension,
                        "candidate_layers": ["L2", "L3", "L4"]
                        if criterion.dimension == "coverage"
                        else ["L4", "L5"],
                        "next_action": criterion.improvement,
                    }
                )
    if row["status"] != "saved":
        signals.append({"code": row["status"], "candidate_layers": []})
    elif not quality.get("complete"):
        signals.append({"code": "review_pending", "candidate_layers": []})
    return signals


def diagnose_batch(batch: Path) -> dict:
    """Recompute from bound originals; no env, model, network or mutation."""
    batch = batch.resolve()
    context = assess_saved_batch(batch)
    trials, criteria, bindings = [], [], {}
    for name in ("plan.json", "suite.json", "index.json"):
        bindings[name] = digest((batch / name).read_bytes())
    for row in context["rows"]:
        task = context["tasks"][row["task_id"]]
        assessment = row["assessment"]
        quality = assessment.get("quality", {})
        run = context["runs"].get(row["id"], {})
        labels, reviewer = {}, {"kind": "unassigned", "name": ""}
        if row["status"] == "saved":
            run_path = Path(row["run_file"]) / "run.json"
            review_path = Path(row["review_file"]).relative_to(batch)
            for path in (run_path, review_path):
                bindings[path.as_posix()] = digest((batch / path).read_bytes())
            packet = json.loads((batch / review_path).read_bytes())
            if check_task_review(run, task, packet) != quality:
                raise ValueError("Review changed during diagnosis; retry with stable saved files")
            reviewer = packet.get("reviewer", reviewer)
            if quality.get("valid") is True:
                labels = {label["id"]: label for label in packet["criteria"]}
        variant = row.get("variant", row["strategy"])
        group = "adversarial" if task.required_exposure else "qa"
        for criterion in task.rubric:
            label = labels.get(criterion.id, {})
            criteria.append(
                {
                    "trial_id": row["id"],
                    "task_id": task.id,
                    "repeat": row["repeat"],
                    "variant": variant,
                    "group": group,
                    "category": task.category,
                    "criterion_id": criterion.id,
                    "dimension": criterion.dimension,
                    "required": criterion.required,
                    "met": label.get("met"),
                    "label_state": "invalid_review"
                    if quality.get("valid") is False
                    else "not_run"
                    if row["status"] != "saved"
                    else "judged"
                    if label.get("met") is not None
                    else "pending",
                    "reason": label.get("reason", ""),
                    "description": criterion.description,
                    "next_action": criterion.improvement,
                    "reviewer_kind": reviewer.get("kind"),
                }
            )
        trials.append(
            {
                "trial_id": row["id"],
                "task_id": task.id,
                "repeat": row["repeat"],
                "variant": variant,
                "group": group,
                "category": task.category,
                "status": row["status"],
                "passed": assessment["passed"],
                "failure_layer": None,
                "signals": trial_signals(row, task),
                "review_valid": quality.get("valid"),
                "review_complete": quality.get("complete"),
                "reviewer": reviewer,
                "question": task.question,
                "answer": run.get("answer"),
                "metrics": run.get("metrics"),
            }
        )
    grouped = {}
    for trial in trials:
        key = trial["group"] + "/" + trial["variant"]
        item = grouped.setdefault(key, {"planned": 0, "passed": 0, "failed": 0, "unassessed": 0})
        item["planned"] += 1
        item[{True: "passed", False: "failed", None: "unassessed"}[trial["passed"]]] += 1
    # Counts are trial counts, not event counts, and candidate layers overlap.
    signals = Counter(code for trial in trials for code in {s["code"] for s in trial["signals"]})
    candidates = Counter(
        layer
        for trial in trials
        for layer in {layer for signal in trial["signals"] for layer in signal["candidate_layers"]}
    )
    return {
        "schema_version": 1,
        "kind": "research_batch_diagnostics",
        "mode": context["plan"]["mode"],
        "bindings": bindings,
        "execution_source_sha256": context["plan"]["source_sha256"],
        "diagnostic_source_sha256": source_digest(),
        "summary": context["summary"],
        "groups": grouped,
        "signals_by_trial": dict(sorted(signals.items())),
        "candidate_layers_by_trial": dict(sorted(candidates.items())),
        "confirmed_failure_layers": {},
        "unlocalized_failed_trials": sum(trial["passed"] is False for trial in trials),
        "trials": trials,
        "criteria": criteria,
        "note": NOTE,
    }


def csv_text(rows: list[dict], fields: list[str]) -> str:
    """UTF-8 CSV with spreadsheet formula/terminal-control neutralization."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        values = {}
        for key in fields:
            value = row.get(key)
            text = (
                "null"
                if value is None
                else str(value).lower()
                if type(value) is bool
                else str(value)
            )
            text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
            if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\n")):
                text = "'" + text
            values[key] = text
        writer.writerow(values)
    return stream.getvalue()


def render_diagnostics(report: dict) -> str:
    def escape(value):
        return html.escape(str(value), quote=True)

    mode_note = {
        "fixture": "固定脚本模型，回答预写，只验证流程。",
        "frozen": "冻结网页与当时真实模型回答；当前仅查看保存结果。",
        "web": "当时真实模型读取指定网页；当前仅查看保存结果。",
        "search": "当时真实模型与公共搜索；当前仅查看保存结果。",
    }.get(report["mode"], "当前仅查看保存结果，模式以原计划为准。")
    lines = [
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>Agent 评测诊断</title><style>body{max-width:1100px;margin:30px auto;padding:0 20px;font:16px/1.7 system-ui}table{border-collapse:collapse;width:100%;margin:20px 0}th,td{border:1px solid #ccc;padding:8px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}small{color:#555}</style>",
        "<h1>Agent 评测诊断</h1>",
        "<p>" + escape(report["note"]) + "</p>",
        "<p>模式：" + escape(report["mode"]) + "。" + escape(mode_note) + "</p>",
        '<p><a href="criteria.csv">逐项长表 criteria.csv</a>（null待定、n/a不适用）</p>',
    ]
    for i, variant in enumerate(sorted(report["summary"]["strategies"]), 1):
        lines.append(f'<p><a href="matrix-{i}.csv">标准矩阵 {i}</a>：' + escape(variant) + "</p>")
    lines += [
        "<h2>配置与任务分组</h2><table><tr><th>分组/配置</th><th>计划</th><th>通过</th><th>失败</th><th>待判定</th></tr>",
    ]
    for key, value in report["groups"].items():
        lines.append(
            "<tr>"
            + "".join(
                "<td>" + escape(v) + "</td>"
                for v in (
                    key,
                    value["planned"],
                    value["passed"],
                    value["failed"],
                    value["unassessed"],
                )
            )
            + "</tr>"
        )
    lines += [
        "</table><h2>预算观察（按原配置，不推算货币费用）</h2><table><tr><th>配置</th><th>耗时中位数ms</th><th>已知用量trial / 计划</th><th>已知token小计</th></tr>",
    ]
    for variant, item in report["summary"]["strategies"].items():
        duration = item["median_duration_ms"]
        lines.append(
            "<tr>"
            + "".join(
                "<td>" + escape(value) + "</td>"
                for value in (
                    variant,
                    f"{duration:.2f}" if duration is not None else "未知",
                    f"{item['usage_known_runs']} / {item['planned']}",
                    item["known_token_subtotal"],
                )
            )
            + "</tr>"
        )
    lines += [
        "</table><h2>可观察信号（每trial最多计一次）</h2><pre>",
        escape(json.dumps(report["signals_by_trial"], ensure_ascii=False, indent=2)),
        "</pre>",
        "<h2>排查候选层：可重叠，不是已确认失败原因</h2><pre>",
        escape(json.dumps(report["candidate_layers_by_trial"], ensure_ascii=False, indent=2)),
        "</pre><p>所有失败仍待定位；不能把这些候选计数相加作为失败总数。</p>",
    ]
    for trial in report["trials"]:
        lines += [
            "<details><summary>"
            + escape(trial["trial_id"])
            + "："
            + escape({True: "通过", False: "失败", None: "待判定"}[trial["passed"]])
            + "</summary><p>"
            + escape(trial["question"])
            + "</p><pre>"
            + escape(trial["answer"] if trial["answer"] is not None else "未保存回答")
            + "</pre><table><tr><th>标准</th><th>标签</th><th>审核者类型</th><th>理由/改进</th></tr>"
        ]
        for criterion in report["criteria"]:
            if criterion["trial_id"] == trial["trial_id"]:
                lines.append(
                    "<tr>"
                    + "".join(
                        "<td>" + escape(v) + "</td>"
                        for v in (
                            criterion["criterion_id"] + "：" + criterion["description"],
                            str(criterion["met"]) + "/" + criterion["label_state"],
                            criterion["reviewer_kind"],
                            criterion["reason"] + "；" + criterion["next_action"],
                        )
                    )
                    + "</tr>"
                )
        lines += ["</table></details>"]
    lines += [
        "<h2>文件绑定</h2><pre>"
        + escape(json.dumps(report["bindings"], indent=2))
        + "</pre></html>"
    ]
    return "\n".join(lines) + "\n"


def write_diagnostics(report: dict, output: Path) -> None:
    """Exclusive new directory; callers choose an allowed output root."""
    output.mkdir(parents=True, exist_ok=False)
    (output / "diagnostics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "index.html").write_text(render_diagnostics(report), encoding="utf-8")
    fields = list(report["criteria"][0]) if report["criteria"] else ["trial_id"]
    (output / "criteria.csv").write_text(csv_text(report["criteria"], fields), encoding="utf-8-sig")
    variants = sorted({row["variant"] for row in report["trials"]})
    for i, variant in enumerate(variants, 1):
        labels = [c for c in report["criteria"] if c["variant"] == variant]
        columns = sorted({c["task_id"] + ":" + c["criterion_id"] for c in labels})
        rows = []
        for trial in report["trials"]:
            if trial["variant"] != variant:
                continue
            values = {
                "trial_id": trial["trial_id"],
                "task_id": trial["task_id"],
                "repeat": trial["repeat"],
            }
            values.update({column: "n/a" for column in columns})
            values.update(
                {
                    c["task_id"] + ":" + c["criterion_id"]: c["met"]
                    for c in labels
                    if c["trial_id"] == trial["trial_id"]
                }
            )
            rows.append(values)
        (output / f"matrix-{i}.csv").write_text(
            csv_text(rows, ["trial_id", "task_id", "repeat", *columns]), encoding="utf-8-sig"
        )
