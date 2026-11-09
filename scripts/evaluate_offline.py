"""Generate reviewable control-flow replay results, NOT a quality benchmark."""

import asyncio
import json
import logging
from pathlib import Path

from as_researcher.core import ResearchConfig
from as_researcher.models import ScriptedModel
from as_researcher.researcher import render_report, run_research
from as_researcher.search import FixtureSearch

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ("normal", {}, "agent_completed", 2),
    ("budget_1", {"max_searches": 1}, "search_budget", 1),
    ("duplicate", {}, "duplicate_query", 1),
    ("duplicate_ablation", {"early_stop_duplicates": False}, "no_new_evidence", 2),
    ("empty", {}, "no_new_evidence", 1),
    ("search_error", {}, "search_error", 1),
    ("invalid_citation", {}, "agent_completed", 2),
    ("injection", {}, "agent_completed", 2),
    ("no_tool", {}, "agent_completed", 0),
]


async def main():
    logging.getLogger("as").setLevel(logging.CRITICAL)
    rows = []
    for name, config, expected_stop, expected_searches in CASES:
        scenario = {"budget_1": "normal", "duplicate_ablation": "duplicate"}.get(name, name)
        model, backend = ScriptedModel(scenario), FixtureSearch(scenario)
        result = await run_research(
            "如何评测 Agent 的记忆能力？",
            model,
            backend,
            ResearchConfig(**config),
            mode="offline_fixture",
        )
        result["fixture_scenario"] = scenario
        result["metrics"]["fixture_model_calls"] = model.calls
        metrics = result["metrics"]
        ok = (
            metrics["stop_reason"] == expected_stop
            and metrics["search_attempts"] == expected_searches
        )
        if name == "invalid_citation":
            ok = ok and metrics["citations"]["invalid_ids"] == ["SRC-00000000"]
        row = {"case": name, "control_check": "PASS" if ok else "FAIL", **metrics}
        rows.append(row)
        target = ROOT / "examples" / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "run.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (target / "report.md").write_text(render_report(result), encoding="utf-8")
    out = ROOT / "examples" / "offline_evaluation.json"
    out.write_text(
        json.dumps(
            {"mode": "synthetic_control_flow_only", "results": rows}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    lines = [
        "# 离线回放结果（模拟数据）",
        "",
        "使用真实 AgentScope 2.0.8 Agent / Toolkit，模型与证据为脚本预置。",
        "这是控制流回归验证，不是模型质量、事实准确率、提示注入防御成功率或真实成本测试。",
        "",
        "| 场景 | 规则检查 | 后端搜索尝试 | 模拟模型调用 | 停止原因 | 未知引用 ID 数 |",
        "|---|---|---:|---:|---|---:|",
    ]
    lines += [
        f"| {r['case']} | {r['control_check']} | {r['search_attempts']} | {r['fixture_model_calls']} | {r['stop_reason']} | {len(r['citations']['invalid_ids'])} |"
        for r in rows
    ]
    lines += [
        "",
        "## 能说明什么",
        "",
        "固定的重复查询样例中，开启查询早停后后端搜索尝试由 2 次降到 1 次；模型调用仍为 3 次。",
        "关闭查询早停时，第二次搜索之后仍会触发‘无新增证据’停止。因此不能声称无限循环或通用成本下降 50%。",
        "injection 场景仅验证恶意文本可以作为数据穿过工具接口；脚本模型不会执行恶意指令，不能用它证明真实模型安全。",
        "invalid_citation 的 PASS 表示成功发现故意植入的错误引用，不表示该报告可以交付。",
        "",
    ]
    (ROOT / "examples" / "OFFLINE_EVALUATION.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        f"{sum(r['control_check'] == 'PASS' for r in rows)}/{len(rows)} control-flow scenarios passed. See {out.parent}"
    )
    if any(r["control_check"] == "FAIL" for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
