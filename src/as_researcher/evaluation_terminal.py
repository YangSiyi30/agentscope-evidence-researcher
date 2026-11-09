"""Guided evaluation over saved evidence and the existing benchmark CLI.

This UI is outside Agent.reply. Merely entering it never loads .env or runs a
model. Completed answers, labels and observations remain separate evidence.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .batch_runtime import recovery_preview
from .benchmark import (
    STRATEGIES,
    ResearchSuite,
    assess_saved_batch,
    check_task_review,
    digest,
    load_suite,
    prepare_task_review,
)
from .core import canonical_url
from .terminal import BackToMenu, ask, choice, number, open_local, show_trace, terminal_text

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "reports/evaluation-20261101/corpus-exposed"
DEFAULT_SUITE = ROOT / "eval/research_suite.json"
GATES = {
    "completed": "模型流程未完整完成",
    "search_finished_without_error": "搜索服务错误",
    "answer_within_limit": "超过配置字符上限",
    "task_answer_within_limit": "超过题目字符上限",
    "search_budget": "搜索次数超限",
    "page_budget": "读页次数超限",
    "saved_evidence": "没有保存可用证据",
    "references_available": "引用未知/层次不符/证据缺失",
    "citations_present": "缺少正文引用",
    "permitted_tools": "请求了未允许的工具",
    "adversarial_payload_exposed": "测试攻击没有进入工具返回",
}


def new_workspace() -> Path:
    target = (
        ROOT
        / "reports/evaluations"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6])
    )
    target.mkdir(parents=True, exist_ok=False)
    return target


def save_json(path: Path, data) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def run_operation(arguments: list[str], workspace: Path, timeout: float = 600,
                  script: str = "benchmark_research.py") -> int:
    """Fixed script/argv, no shell; logs stay in this evaluation workspace."""
    if script not in {"benchmark_research.py", "compare_search.py"}:
        raise ValueError("Unknown fixed evaluation script")
    command = [sys.executable, str(ROOT / "scripts" / script), *arguments]
    print("\n正在执行：" + terminal_text(" ".join(arguments)) + "；不会自动重试。", flush=True)
    # Keep SDK/provider bodies out of the menu. The runner already suppresses
    # worker payloads; show validated artifacts rather than arbitrary stderr.
    stem = "operation-" + uuid4().hex[:6]
    with (
        (workspace / (stem + ".stdout.txt")).open("w", encoding="utf-8") as stdout,
        (workspace / (stem + ".stderr.txt")).open("w", encoding="utf-8") as stderr,
    ):
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                timeout=timeout,
            )
            code = result.returncode
        except subprocess.TimeoutExpired:
            print("等待子进程超过上限；已停止直接子进程，请核对保存的index，未自动重跑。")
            code = 2
    print(f"操作退出码：{code}；日志目录：" + terminal_text(workspace))
    if code == 2:
        print("配置/绑定/运行未完成；可查看本目录stderr日志和批次index，不把它解释为知识得分。")
    elif code == 3:
        print("成绩中存在失败或待判定，或复核尚未完成；请看具体检查项。")
    return code


def required_text(prompt: str, minimum: int, maximum: int) -> str:
    while True:
        value = ask(prompt)
        if minimum <= len(value) <= maximum:
            return value
        print(f"请输入{minimum}..{maximum}个字符；q返回主页。")


def custom_suite(
    question: str, criteria: list[str], document: str, url: str, maximum: int
) -> ResearchSuite:
    """Create a new task; never change the built-in bank or its past rubrics."""
    return ResearchSuite.model_validate(
        {
            "schema_version": 1,
            "name": "terminal-custom-development",
            "provenance": "用户在终端输入的问题与标准；已用于开发，不是独立holdout或专家gold。",
            "documents": {document: url},
            "tasks": [
                {
                    "id": "custom_question",
                    "category": "用户定向开发测试",
                    "question": question
                    + f" 请仅依据本次保存证据作答；非空白字符不超过{maximum}（含引用）；缺少依据请明确说明。",
                    "documents": [document],
                    "max_answer_chars": maximum,
                    "rubric": [
                        {
                            "id": f"point_{i}",
                            "dimension": "coverage",
                            "description": text,
                            "improvement": "针对缺失的必答点检查选段与回答，依据原件修正。",
                        }
                        for i, text in enumerate(criteria, 1)
                    ]
                    + [
                        {
                            "id": "saved_support",
                            "dimension": "support",
                            "description": "主要结论由本次实际保存证据支持，引用层次正确，保留条件和否定范围。",
                            "improvement": "对齐原回答与本次保存的正文或摘要，不用事后网页补证据。",
                        }
                    ],
                }
            ],
        }
    )


def create_custom() -> tuple[Path, str, Path | None]:
    print("\n先想清楚想找什么问题：缺一侧比较、否定扩大、还是回答超长。标准写可核对的事实要求。")
    question = required_text("研究问题（10..1800字符）：", 10, 1800)
    maximum = number("回答非空白字符上限（含引用）", 500, 100, 10000)
    count = number("必答点条数（另自动增加一条证据支持标准）", 2, 1, 5)
    criteria = [
        required_text(f"必答点{i}（例如：必须说明取消对象是aw）：", 8, 1000)
        for i in range(1, count + 1)
    ]
    selected = choice(
        "测试资料",
        {
            "1": "已保存Python 3.13官方页（无需下载）",
            "2": "已保存MCP 2025-11-25官方页",
            "3": "提供新的公开HTML URL",
        },
        "1",
    )
    base = load_suite(DEFAULT_SUITE)
    if selected in {"1", "2"}:
        document = "python" if selected == "1" else "mcp_new"
        url, corpus = base.documents[document], CORPUS
    else:
        document = "custom_page"
        while True:
            url = required_text("公开HTML URL（不支持PDF/登录/动态点击）：", 10, 2000)
            normalized = canonical_url(url)
            if normalized:
                url = normalized
                break
            print("请使用规范的公开http(s)地址，不含凭据、私网IP或片段；不要粘贴多个地址。")
        corpus = None
    suite = custom_suite(question, criteria, document, url, maximum)
    workspace = new_workspace()
    path = workspace / "custom-suite.json"
    save_json(path, suite.model_dump())
    print("已保存新题库：" + terminal_text(path))
    print("原预设题库和历史评分标准未修改。更改问题/标准应重新建立实验，不能沿用旧分数。")
    return path, "custom_question", corpus


def select_task() -> str:
    suite = load_suite(DEFAULT_SUITE)
    options = {
        str(i): terminal_text(t.id + "：" + t.question[:90]) for i, t in enumerate(suite.tasks, 1)
    }
    selected = choice("选择一道预设题（先一题定位问题，再扩大范围）", options | {"0": "返回"}, "1")
    if selected == "0":
        raise BackToMenu
    return suite.tasks[int(selected) - 1].id


def configure(suite: Path, case: str, corpus: Path | None) -> list[str]:
    mode = choice(
        "运行环境",
        {
            "1": "真实模型＋冻结网页（同资料对照，可能计费）",
            "2": "真实模型＋实际URL（验证实时读页）",
            "3": "真实模型＋公共搜索（仅用搜索结果，不限定所填URL）",
            "4": "固定模型＋冻结网页（仅验接线，不测智能）",
        },
        "1",
    )
    axis = choice(
        "本次只变化哪一项？",
        {
            "1": "单配置：segments＋standard",
            "2": "比较选段：bm25 vs balanced，统一standard",
            "3": "比较格式：standard vs compact，统一segments",
        },
        "1",
    )
    repeat = number("每个配置重复次数", 1, 1, 2)
    page_chars = number("每页正文字符预算", 2400, 100, 12000)
    answer_chars = number("回答字符上限（仍与题目上限取较小值）", 500, 100, 10000)
    args = [
        "--suite",
        str(suite),
        "--case",
        case,
        "--mode",
        {"1": "frozen", "2": "web", "3": "search", "4": "fixture"}[mode],
        "--repeats",
        str(repeat),
        "--chars-per-page",
        str(page_chars),
        "--answer-chars",
        str(answer_chars),
    ]
    args += (
        ["--strategy", "bm25", "--strategy", "balanced"]
        if axis == "2"
        else ["--strategy", "segments"]
    )
    if axis == "3":
        args += ["--answer-style", "standard", "--answer-style", "compact"]
    if mode in {"1", "4"} and corpus:
        args += ["--corpus", str(corpus)]
    if mode in {"2", "3"}:
        transport = choice("工具传输", {"1": "local：直接调用", "2": "MCP stdio"}, "1")
        args += ["--transport", "local" if transport == "1" else "mcp"]
    if mode == "3":
        provider = choice(
            "公共搜索服务（不自动切换）", {"1": "DuckDuckGo", "2": "Tavily，需要本机key"}, "1"
        )
        args += ["--search-provider", "duckduckgo" if provider == "1" else "tavily"]
    return args


def perform_evaluation(arguments: list[str], workspace: Path) -> int:
    # Save exactly the displayed current plan before deciding to execute.
    plan_code = run_operation(["plan", *arguments], workspace)
    if plan_code:
        return plan_code
    logs = sorted(workspace.glob("operation-*.stdout.txt"), key=lambda p: p.stat().st_mtime_ns)
    plan = json.loads(logs[-1].read_bytes())
    save_json(workspace / "preview-plan.json", plan)
    print(
        f"\n计划：{plan['mode']}；{len(plan['trials'])}次trial；最多{plan['live_model_trials']}个真实模型研究任务；每题可能多次请求。"
    )
    print("配置：" + terminal_text(str(plan["strategies"]) + " / " + str(plan["answer_styles"])))
    print(f"读页字符≤{plan['chars_per_page']}；题目和配置回答上限取较小值；0重试、缩稿关闭。")
    print("保存的计划：" + terminal_text(workspace / "preview-plan.json"))
    if plan["mode"] in {"fixture", "frozen"} and plan["documents"] and not plan["corpus"]:
        print("需要先保存新的公开HTML。下载不是模型调用，但会访问所填URL。")
        if ask("输入 FETCH 下载；回车仅保留计划：").upper() != "FETCH":
            return 0
        corpus = workspace / "corpus"
        code = run_operation(["fetch", *arguments, "--output", str(corpus)], workspace)
        if code:
            return code
        arguments = [*arguments, "--corpus", str(corpus)]
        return perform_evaluation(arguments, workspace)
    if plan["mode"] == "fixture":
        print("固定模型只演示接线；回答预写，不能拿它评估新问题或注入防御。")
        if choice("运行固定流程？", {"1": "运行（无模型费）", "0": "仅保留计划"}, "1") == "0":
            return 0
    elif ask("输入 LIVE 才运行真实模型（可能计费）；回车仅保留计划：").upper() != "LIVE":
        return 0
    batch = workspace / "batch"
    code = run_operation(
        ["run", *arguments, "--execute", "--output", str(batch)],
        workspace,
        len(plan["trials"]) * (plan["timeout"] + 45) + 30,
    )
    if (batch / "index.json").exists():
        view_batch(batch)
    return code


def describe_trial(row: dict, task) -> list[str]:
    """Map independent operational/semantic failures to bounded next actions."""
    assessment = row.get("assessment", {})
    operational = assessment.get("operational", {}).get("checks", {})
    lines = ["运行门槛：" + GATES.get(k, k) for k, v in operational.items() if v is False]
    quality = assessment.get("quality", {})
    if quality.get("valid") is False:
        lines.append("复核文件绑定/格式无效：" + ", ".join(quality.get("errors", [])))
    failed = quality.get("failed_required", [])
    lines += [
        "未满足标准：" + c.description + "；建议：" + c.improvement
        for c in task.rubric
        if c.id in failed
    ]
    if not quality.get("complete") and row["status"] == "saved":
        lines.append(
            "语义复核未完成：先读原回答与保存证据，再填满足/不满足/待判定；程序不会自动判事实。"
        )
    if any(
        operational.get(k) is False for k in ("answer_within_limit", "task_answer_within_limit")
    ):
        lines.append("改进方向：先比较compact格式；保留关键事实/条件和引用，不用直接截断回答。")
    if operational.get("saved_evidence") is False:
        lines.append("改进方向：先看trace区分搜索/读页错误；无证据时不要先改回答提示。")
    if row["status"] != "saved":
        lines.append("本次没有可用run；检查index与操作日志，保留未运行行，不当成知识零分。")
    return lines


def show_batch(context: dict, batch: Path) -> None:
    print("\n评测批次：" + terminal_text(batch))
    print(
        "模式："
        + terminal_text(context["plan"]["mode"])
        + "；程序门槛、语义复核、最终判定分别统计。"
    )
    print("配置 | 计划 | 门槛通过 | 完整复核 | 通过 / 失败 / 待判定")
    for variant, summary in context["summary"]["strategies"].items():
        print(
            terminal_text(
                f"{variant} | {summary['planned']} | {summary['operational_passes']} | {summary['reviewed']} | {summary['passed']} / {summary['failed']} / {summary['unassessed']}"
            )
        )
    if context["plan"]["mode"] == "fixture":
        print("此批是固定模型流程，不能解读为研究能力得分。")
    for i, row in enumerate(context["rows"], 1):
        verdict = {True: "通过", False: "失败", None: "待判定"}[row["assessment"]["passed"]]
        print(
            terminal_text(
                f"  {i}. {row['id']}：{verdict}；{row['status']}；字符{row.get('metrics', {}).get('answer_chars', '未知')}"
            )
        )


def review_trial(run: dict, task, folder: Path) -> bool:
    print("\n你正在亲自复核，标签将如实记为human；本程序不会替你判断。先核对上方原回答和实际证据。")
    name = required_text("审核者署名（不要用改名冒充多人）：", 1, 100)
    packet = prepare_task_review(run, task)
    packet["reviewer"] = {"kind": "human", "name": name}
    evidence = [
        ({"source_id": p["id"], "kind": kind}, p["content"])
        for kind, field in (("snippet", "sources"), ("page", "page_evidence"))
        for p in run.get(field, [])
        if p.get("content", "").strip()
    ]
    for criterion, label in zip(task.rubric, packet["criteria"]):
        print("\n标准：" + terminal_text(criterion.description))
        selected = choice(
            "依据原回答和原证据判断", {"1": "满足", "2": "不满足", "3": "尚不能判断"}, "3"
        )
        if selected == "3":
            continue
        label.update(
            met=selected == "1",
            reason=required_text("判断理由：", 1, 2000),
            answer_quote=run["answer"],
        )
        if selected == "1" and criterion.dimension == "support":
            if not evidence:
                print("没有可定位证据，不能将支持项记满足；本项保持待判定。")
                label["met"] = None
                continue
            selected_evidence = choice(
                "选择实际支撑该结论的证据",
                {
                    str(i): terminal_text(meta["source_id"] + "@" + meta["kind"] + " " + text[:90])
                    for i, (meta, text) in enumerate(evidence, 1)
                },
                "1",
            )
            meta, text = evidence[int(selected_evidence) - 1]
            print(terminal_text(text))
            while True:
                quote = ask("粘贴支持原文中的连续摘录（回车用本次整段定位）：") or text
                if quote in text:
                    label["evidence"] = [{**meta, "quote": quote}]
                    break
                print("摘录不在所选证据中，请重新输入；不要用事后资料补证据。")
    packet["coverage_checked"] = True
    checked = check_task_review(run, task, packet)
    if not checked["valid"]:
        print("复核无效，未保存：" + terminal_text(checked["errors"]))
        return False
    print(
        "将保存你的判断；完整复核：" + str(checked["complete"]) + "。unknown仍保留，不自动当成功。"
    )
    if ask("输入 SAVE 保存复核；回车取消：").upper() != "SAVE":
        return False
    path = folder / "task-review.json"
    if path.exists():
        history = folder / "review-history"
        history.mkdir(exist_ok=True)
        (history / (uuid4().hex + ".json")).write_bytes(path.read_bytes())
    save_json(path, packet)
    print("已保存：" + terminal_text(path) + "；已有复核已另存历史，run/原回答未修改。")
    return True


def view_trial(context: dict, row: dict, batch: Path, readonly: bool = False) -> None:
    task = context["tasks"][row["task_id"]]
    for line in describe_trial(row, task):
        print(terminal_text(line))
    if row["status"] != "saved":
        return
    run, folder = context["runs"][row["id"]], batch / row["run_file"]
    print(
        "\n问题："
        + terminal_text(task.question)
        + "\n\n完整回答：\n"
        + terminal_text(run["answer"])
    )
    for warning in run["metrics"].get("warnings", []):
        print("运行提示：" + terminal_text(warning))
    while True:
        selected = choice(
            "本次试验",
            {
                "1": "查看工具trace",
                "2": "查看保存证据与选段（定位漏答）",
                "3": "查看评分标准、现有标签和理由",
                **({} if readonly else {"4": "亲自填写任务复核"}),
                "5": "默认打开report.md",
                "6": "默认打开run.json",
                "7": "打开本次文件夹",
                "0": "返回批次",
            },
            "0",
        )
        if selected == "0":
            return
        if selected == "1":
            show_trace(run)
        elif selected == "2":
            for kind, field in (("搜索摘要", "sources"), ("正文片段", "page_evidence")):
                for page in run.get(field, []):
                    print(
                        terminal_text(
                            f"\n{kind} {page['id']} {page.get('url', '')}\n{page.get('content') or '仅登记URL，未取得摘要'}"
                        )
                    )
            print(
                "回到trace看focus、读取错误与预算；关键句未返回先查选择器，已返回却漏答再查模型表达。"
            )
        elif selected == "3":
            review = json.loads(Path(row["review_file"]).read_bytes())
            print("审核者：" + terminal_text(review.get("reviewer")))
            for criterion, label in zip(task.rubric, review["criteria"]):
                print(
                    terminal_text(
                        f"{criterion.id} 必需={criterion.required}；{criterion.description}\n标签={label['met']}；理由={label['reason']}\n改进：{criterion.improvement}"
                    )
                )
        elif selected == "4":
            if review_trial(run, task, folder):
                return
        else:
            path = (
                folder / {"5": "report.md", "6": "run.json"}[selected]
                if selected != "7"
                else folder
            )
            try:
                open_local(path)
            except (OSError, subprocess.SubprocessError):
                print("打开失败，可复制路径手动查看：" + terminal_text(path))


def available_corpus(plan: dict) -> Path | None:
    """Relocate saved examples by matching bytes, not by trusting an old path."""
    candidates = [Path(plan["corpus"])] if plan.get("corpus") else []
    candidates.append(CORPUS)
    for candidate in candidates:
        candidate = candidate.resolve()
        if not candidate.is_relative_to(ROOT.resolve()):
            continue
        path = candidate / "manifest.json"
        if path.is_file() and digest(path.read_bytes()) == plan.get("corpus_manifest_sha256"):
            return candidate
    return None


def view_batch(batch: Path, readonly: bool = False) -> None:
    while True:
        context = assess_saved_batch(batch)
        show_batch(context, batch)
        if readonly:
            print("随源码携带的历史真实样例（只读、assistant标签）；查看不请求模型，r才建立新实验。")
        selected = choice(
            "只读样例：数字看原回答，c对照，d导出诊断，r准备新实验" if readonly
            else "输入序号看单次完整回答；s生成可打开的Markdown成绩单",
            {str(i): row["id"] for i, row in enumerate(context["rows"], 1)}
            | {
                "c": "同题A/B完整回答对照",
                "r": "复用某题和标准，建立新实验（先预览）",
                **({} if readonly else {"s": "生成成绩单（仅本地，不运行模型）"}),
                "d": "导出诊断网页与标准矩阵（仅本地，不运行模型）",
                **({} if readonly else {
                    "u": "预览续跑：保留已保存，只补未启动试验",
                    "o": "打开最新成绩单",
                }),
                "0": "返回评测中心",
            },
            "0",
        )
        if selected == "0":
            return
        if selected == "c":
            show_pairs(context)
            continue
        if selected == "u":
            try:
                preview = recovery_preview(batch, ROOT / "scripts/benchmark_research.py")
            except (ValueError, OSError, KeyError) as exc:
                print("不能续跑：" + terminal_text(str(exc)) + "；旧批次仍可只读，r可新建实验。")
                continue
            print(terminal_text({"已保存": len(preview["saved"]), "仅恢复凭据": list(preview["recoverable"]),
                                 "将启动": preview["pending"], "结果不确定，不重试": preview["uncertain"]}))
            print("只有全新未启动的试验会执行；这不是Agent状态恢复，已启动但结果不明可能已有费用。")
            if not preview["pending"] and not preview["recoverable"]:
                continue
            if preview["pending"] and preview["mode"] != "fixture":
                if ask("输入 LIVE 才启动剩余真实模型试验；回车取消：").upper() != "LIVE":
                    continue
            elif choice("提交本次续跑？", {"1": "继续（不请求真实模型）", "0": "取消"}, "0") != "1":
                continue
            workspace = new_workspace()
            count = max(1, len(preview["pending"]))
            code = run_operation(["resume", "--batch", str(batch), "--execute"], workspace,
                                 count * (preview["context"]["plan"]["timeout"] + 80) + 30)
            print("续跑操作退出码：" + str(code) + "；保留不确定行，请重新查看本批状态。")
            continue
        if selected == "r":
            tasks = list(context["tasks"].values())
            value = choice(
                "复用哪道题？原批次和标准不会修改",
                {
                    str(i): terminal_text(t.id + "：" + t.question[:75])
                    for i, t in enumerate(tasks, 1)
                }
                | {"0": "返回"},
                "0",
            )
            if value != "0":
                corpus = available_corpus(context["plan"])
                arguments = configure(batch / "suite.json", tasks[int(value) - 1].id, corpus)
                perform_evaluation(arguments, new_workspace())
            continue
        if selected == "s":
            workspace = new_workspace()
            run_operation(["score", "--batch", str(batch)], workspace)
            continue
        if selected == "d":
            workspace = new_workspace()
            output = workspace / "diagnostics"
            code = run_operation(["diagnose", "--batch", str(batch), "--output", str(output)], workspace)
            if code == 0:
                print("诊断已保存：" + terminal_text(output))
                print("index.html看全貌；criteria.csv筛选标准；matrix-N.csv按配置查看。规则候选不是根因。")
                try:
                    open_local(output / "index.html")
                except (OSError, subprocess.SubprocessError):
                    print("打开失败，可复制路径：" + terminal_text(output / "index.html"))
            continue
        if selected == "o":
            reports = sorted(batch.glob("assessment-*.md"))
            if reports:
                try:
                    open_local(reports[-1])
                except (OSError, subprocess.SubprocessError):
                    print("打开失败，可复制路径：" + terminal_text(reports[-1]))
            else:
                print("尚无成绩单，请先选s生成。查看和评分不会重新请求模型。")
            continue
        view_trial(context, context["rows"][int(selected) - 1], batch, readonly=readonly)


def show_pairs(context: dict) -> None:
    pairs = context["summary"]["pairs"]
    if not pairs:
        print("本批没有两种配置的成对试验；单配置可逐题看失败原因。")
        return
    selected = choice(
        "选择同题/同重复号的一对",
        {str(i): f"{p['task_id']} 第{p['repeat']}次" for i, p in enumerate(pairs, 1)}
        | {"0": "返回"},
        "1",
    )
    if selected == "0":
        return
    pair = pairs[int(selected) - 1]
    print("先比较证据是否取得，再看必答点与支持，最后比较长度/用量；小样本不证明普遍优越。")
    for variant in (pair["left"], pair["right"]):
        row = next(
            r
            for r in context["rows"]
            if r["task_id"] == pair["task_id"]
            and r["repeat"] == pair["repeat"]
            and r.get("variant", r["strategy"]) == variant
        )
        print("\n" + terminal_text(variant))
        for line in describe_trial(row, context["tasks"][row["task_id"]]):
            print(terminal_text(line))
        if row["id"] in context["runs"]:
            run = context["runs"][row["id"]]
            print(
                terminal_text(
                    f"字符={run['metrics']['answer_chars']}；token={run['metrics'].get('token_usage')}；focus={[e.get('focus') for e in run['trace'] if e.get('tool') == 'read_page']}\n"
                    + run["answer"]
                )
            )


def show_extraction(folder: Path) -> None:
    result = json.loads((folder / "results.json").read_bytes())
    rows = result["rows"]
    for strategy in STRATEGIES:
        values = [r for r in rows if r["strategy"] == strategy]
        print(
            f"{strategy}: 字面目标共现{sum(r['all_targets_hit'] is True for r in values)}/{sum(r['all_targets_hit'] is not None for r in values)}；待定{sum(r['all_targets_hit'] is None for r in values)}"
        )
    print(
        "仅字面共现，非问答准确率；目标不在原文则null。结果："
        + terminal_text(folder / "results.json")
    )
    while True:
        cases = list(dict.fromkeys(r["case"] for r in rows))
        selected = choice(
            "查看哪一场景的预算/命中与实际片段？",
            {str(i): c for i, c in enumerate(cases, 1)} | {"0": "返回"},
            "0",
        )
        if selected == "0":
            return
        case_rows = [r for r in rows if r["case"] == cases[int(selected) - 1]]
        for i, row in enumerate(case_rows, 1):
            print(
                terminal_text(
                    f"{i}. {row['strategy']}；预算{row['budget']}；字符{row['chars']}；命中{row['all_targets_hit']}；目标在原文={row['target_in_page']}"
                )
            )
        selected_row = number("要查看的行号（0返回场景）", 0, 0, len(case_rows))
        if selected_row:
            row = case_rows[selected_row - 1]
            print(
                "focus："
                + terminal_text(row["focus"])
                + "\n实际返回：\n"
                + terminal_text(row["selected"]["content"])
            )
            print("目标存在却未选到，先改focus/排序/分配；小预算若放不下完整代码，应保留省略诊断。")


def browse_batches() -> None:
    batches = sorted(
        (
            p.parent
            for p in (ROOT / "reports").rglob("index.json")
            if (p.parent / "suite.json").is_file() and (p.parent / "plan.json").is_file()
        ),
        key=lambda p: (p / "index.json").stat().st_mtime,
        reverse=True,
    )[:30]
    if not batches:
        print("暂无评测批次。选1先运行一次固定离线示例。")
        return
    selected = choice(
        "最近30组以内的问答评测（查看不请求模型）",
        {str(i): terminal_text(p.relative_to(ROOT / "reports")) for i, p in enumerate(batches, 1)}
        | {"e": "查看已有抽取对照", "0": "返回"},
        "0",
    )
    if selected == "e":
        files = sorted(
            (ROOT / "reports").rglob("results.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        folders = []
        for path in files:
            try:
                if json.loads(path.read_bytes()).get("kind") == "research_extraction_ablation":
                    folders.append(path.parent)
            except (ValueError, OSError, AttributeError):
                continue
        if folders:
            value = choice(
                "抽取对照（只读）",
                {
                    str(i): terminal_text(p.relative_to(ROOT / "reports"))
                    for i, p in enumerate(folders[:20], 1)
                }
                | {"0": "返回"},
                "0",
            )
            if value != "0":
                show_extraction(folders[int(value) - 1])
        else:
            print("暂无抽取结果。")
    elif selected != "0":
        view_batch(batches[int(selected) - 1])


def run_evaluation_center() -> int | None:
    print("\n评测中心：先选固定流程或具体研究题，再固定资料/变量，最后查每次回答与证据。")
    last_code = None
    while True:
        try:
            selected = choice(
                "评测中心",
                {
                    "1": "固定离线流程测试（1题1次，无key无模型费）",
                    "2": "预设研究题：计划 / 冻结网页 / 实时测试",
                    "3": "自定义问题、必答点与资料",
                    "4": "离线抽取对照（只测选段，不测回答）",
                    "5": "查看已有批次、失败原因 / 复核 / 成绩单",
                    "6": "操作说明",
                    "7": "查看随源码提供的真实A/B样例（只读、无模型费）",
                    "8": "同查询对照DuckDuckGo/Tavily（先预览，搜索可能计费）",
                    "0": "返回主页",
                },
                "1",
            )
            if selected == "0":
                return last_code
            if selected == "5":
                browse_batches()
            elif selected == "7":
                view_batch(ROOT / "examples/portfolio/batch", readonly=True)
            elif selected == "8":
                workspace = new_workspace()
                code = run_operation([], workspace, script="compare_search.py")
                if code:
                    continue
                path = max(workspace.glob("operation-*.stdout.txt"), key=lambda p: p.stat().st_mtime)
                plan = json.loads(path.read_bytes())
                print("固定对照查询：" + terminal_text([c["query"] for c in plan["suite"]["queries"]]))
                print(f"最多{plan['logical_searches']}次逻辑搜索，其中{plan['tavily_basic_requests']}次Tavily basic可能消耗积分；无模型、无正文读取、无自动重试。")
                if ask("输入 SEARCH 才进行真实搜索；回车仅保留计划：").upper() != "SEARCH":
                    continue
                output = workspace / "search-comparison"
                last_code = run_operation(["--execute", "--plan", str(path), "--output", str(output)], workspace,
                                          timeout=300, script="compare_search.py")
                if (output / "results.json").is_file():
                    report = json.loads((output / "results.json").read_bytes())
                    print(terminal_text(report["providers"]))
                    print("目标主机命中不等于事实正确；结果仅代表本次网络/时点。报告：" + terminal_text(output / "report.md"))
            elif selected == "6":
                print("从start.bat主页选4进入本中心。建议顺序1→5→2或3；4专门定位选段。")
                print("待判定不等于失败；completed不等于事实正确。复核填完后用s更新成绩单，o打开。")
                print(
                    "完整操作与具体例子：" + str(ROOT / "docs/INTERVIEW_GUIDE_ZH.md") + " 第16节。"
                )
            elif selected == "4":
                workspace = new_workspace()
                code = run_operation(
                    [
                        "ablate",
                        "--corpus",
                        str(CORPUS),
                        "--extraction-plan",
                        str(ROOT / "eval/coverage_suite.json"),
                        "--output",
                        str(workspace / "extraction"),
                    ],
                    workspace,
                )
                last_code = code
                if code == 0:
                    show_extraction(workspace / "extraction")
            else:
                if selected == "3":
                    suite, case, corpus = create_custom()
                    workspace = suite.parent
                else:
                    workspace = new_workspace()
                    suite, case, corpus = (
                        DEFAULT_SUITE,
                        "mcp_stdio" if selected == "1" else select_task(),
                        CORPUS,
                    )
                arguments = (
                    [
                        "--suite",
                        str(suite),
                        "--case",
                        case,
                        "--mode",
                        "fixture",
                        "--strategy",
                        "segments",
                        "--repeats",
                        "1",
                        "--corpus",
                        str(CORPUS),
                        "--answer-chars",
                        "500",
                    ]
                    if selected == "1"
                    else configure(suite, case, corpus)
                )
                last_code = perform_evaluation(arguments, workspace)
        except BackToMenu:
            print("已取消当前评测设置；已保存材料保留。")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            print(
                "评测设置/材料无法读取："
                + terminal_text(type(exc).__name__)
                + "。请检查题库、快照及绑定；未自动重试。"
            )
