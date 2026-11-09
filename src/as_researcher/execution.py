"""Deterministic execution bookkeeping; never infer counts from model prose."""


def _count(value):
    return value if type(value) is int and value >= 0 else None


def execution_summary(run: dict) -> dict:
    metrics = run.get("metrics", {})
    counts = {
        key: _count(metrics.get(key))
        for key in ("tool_invocations", "search_attempts", "page_read_attempts", "pages_read")
    }
    counts["blocked_requests"] = None
    issues = []
    trace = run.get("trace")
    if isinstance(trace, list) and counts["tool_invocations"] is not None:
        if len(trace) != counts["tool_invocations"]:
            counts["tool_invocations"] = None
            issues.append("工具请求总数与Trace长度不一致。")
        elif run.get("execution_trace_version") == 1 and all(
            isinstance(e, dict)
            and e.get("tool") in {"search_web", "read_page"}
            and type(e.get("backend_attempted")) is bool
            for e in trace
        ):
            counts["blocked_requests"] = sum(not e["backend_attempted"] for e in trace)
            for tool, key in (
                ("search_web", "search_attempts"),
                ("read_page", "page_read_attempts"),
            ):
                observed = sum(e["backend_attempted"] for e in trace if e["tool"] == tool)
                if counts[key] is not None and counts[key] != observed:
                    counts[key] = None
                    issues.append("后端尝试计数与Trace不一致：" + key)
    pages = run.get("page_evidence")
    if (
        isinstance(pages, list)
        and counts["pages_read"] is not None
        and len(pages) != counts["pages_read"]
    ):
        counts["pages_read"] = None
        issues.append("保存正文数与页面记录不一致。")
    return {"counts": counts, "issues": issues}


def render_execution_summary(run: dict, *, report_version: int = 2) -> list[str]:
    summary = execution_summary(run)
    lines = ["", "## 程序执行摘要", "", "| 项目 | 次数 |", "|---|---:|"]
    for label, key in (
        ("已记录工具请求", "tool_invocations"),
        ("后端搜索尝试", "search_attempts"),
        ("后端读页尝试", "page_read_attempts"),
        ("被拦截、未进入后端的请求", "blocked_requests"),
        ("已保存正文片段", "pages_read"),
    ):
        value = summary["counts"][key]
        lines.append(f"| {label} | {value if value is not None else '未记录或无法核对'} |")
    lines += [
        "",
        "以上来自程序运行记录，不依赖下方模型自述。后端尝试包含失败与取消；这些次数不是HTTP请求数。未进入应用工具的SDK拦截不在此表内。",
    ]
    lines.extend("> 执行记录校验：" + issue for issue in summary["issues"])
    # Preserve v2 output verbatim. The saved answer may instead be a program
    # placeholder after failure, or an accepted revision rather than the draft.
    lines += ["", "## 保存的回答" if report_version >= 3 else "## 模型回答（原文）"]
    if report_version >= 3:
        lines += [
            "",
            "> 以下原样展示run.answer；它可能是模型回答、已采用的缩稿候选，或未完成运行的程序提示。原稿与候选见revision记录。",
        ]
    return lines
