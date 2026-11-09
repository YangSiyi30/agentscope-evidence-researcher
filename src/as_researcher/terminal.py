"""Small terminal UI over the existing one-task CLI, with read-only run inspection."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .cli import create_parser, execute_args, result_exit_code
from .execution import execution_summary
from .source_inputs import normalize_source_urls

DEFAULT_TOPIC = "解释检索相关性与事实支持的区别"
LIVE_TOPIC = "根据给定文档，说明 asyncio.wait 设置 timeout 后如何处理未完成的任务，以及 done/pending 分别表示什么。请引用保存的正文。"
LIVE_URL = "https://docs.python.org/3/library/asyncio-task.html"
VIEWER_ENV_NAMES = {
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERNAME",
    "USERDOMAIN",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "DBUS_SESSION_BUS_ADDRESS",
}


class BackToMenu(Exception):
    """Cancel setup before any model/tool construction."""


def terminal_text(value: object) -> str:
    """External answers/titles must not execute terminal control sequences."""
    text = str(value)
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) <= 159))


def ask(prompt: str) -> str:
    value = input(prompt).strip()
    if value.casefold() == "q":
        raise BackToMenu
    return value


def choice(prompt: str, options: dict[str, str], default: str) -> str:
    print("\n" + prompt)
    for key, label in options.items():
        print(f"  {key}. {label}")
    while True:
        value = ask(f"选择 [{default}]：") or default
        if value in options:
            return value
        print("请输入菜单中的编号；q 返回主页。")


def number(label: str, default: int, minimum: int, maximum: int) -> int:
    while True:
        raw = ask(f"{label}（{minimum}..{maximum}）[{default}]：")
        try:
            value = int(raw) if raw else default
            if minimum <= value <= maximum:
                return value
        except ValueError:
            pass
        print("请输入范围内的整数。")


def collect_task(live: bool, output_dir: Path) -> list[str]:
    """Build ordinary CLI arguments. No dotenv, client, network or file writes."""
    print("\n设置时输入 q 返回主页。每题新建 Session，不继承上一题的证据或预算。")
    transport = choice(
        "工具传输", {"1": "local：直接调用（推荐先学）", "2": "MCP：本地 stdio 子进程"}, "1"
    )
    reader = "http"
    if live:
        selected = choice(
            "正文读取方式",
            {
                "1": "HTTP：读取静态网页",
                "2": "浏览器：隔离 Edge 静态 DOM",
                "3": "不读正文，仅搜索摘要",
            },
            "1",
        )
        reader = {"1": "http", "2": "browser", "3": "none"}[selected]
    else:
        print("离线使用固定模型、固定搜索与正文；不会启动浏览器或访问公共网络。")
    searches, pages, timeout, answer_chars, tokens = (
        (0 if live and reader != "none" else 1),
        (0 if reader == "none" else 1),
        120,
        700,
        1800,
    )
    advanced = choice(
        "预算",
        {
            "1": f"推荐小预算：搜索{searches}次 / 读页{pages}次 / 120秒 / 回答700字",
            "2": "自己设置预算",
        },
        "1",
    )
    if advanced == "2":
        searches = number("搜索尝试上限（失败也计数）", searches, 0, 10)
        pages = number("读页尝试上限", pages, 0, 5) if reader != "none" else 0
        timeout = number("主循环秒数", 120, 1, 600)
        answer_chars = number("回答非空白字符上限", 700, 100, 10000)
        if live:
            tokens = number("每次模型输出 token 上限", 1800, 64, 4096)
    if searches == 0 and pages == 0:
        print("搜索和读页不能同时为0：本助手需要外部证据。请重新设置。")
        raise BackToMenu
    args = [
        "--transport",
        "mcp" if transport == "2" else "local",
        "--reader",
        reader,
        "--searches",
        str(searches),
        "--pages",
        str(pages),
        "--timeout",
        str(timeout),
        "--max-answer-chars",
        str(answer_chars),
        "--strict",
        "--output-dir",
        str(output_dir),
    ]
    if not live:
        demos = {
            "1": "预设：检索相关性与事实支持（搜索→读页→回答）"
            if pages
            else "预设：固定搜索→摘要回答（不读正文）",
            "2": "预设：重复搜索拦截",
            "3": "预设：没有搜到证据",
            "4": "输入自己的问题（仍返回固定演示内容）",
        }
        if searches == 0:
            demos = {"1": "预设：给定 URL 的固定读页演示（0搜索）", "4": demos["4"]}
        selected = choice("想演示什么？", demos, "1")
        scenario = {"1": "tool_chain", "2": "duplicate", "3": "empty", "4": "tool_chain"}[selected]
        if not pages and scenario == "tool_chain":
            scenario = "normal"
        if selected == "2" and searches < 2:
            if (
                choice(
                    "去重演示需要至少2次搜索预算，是否将本题预算改为2？",
                    {"1": "改为2（重复请求会被拦截）", "0": "返回主页"},
                    "1",
                )
                == "0"
            ):
                raise BackToMenu
            args[args.index("--searches") + 1] = "2"
        topic = {
            "1": DEFAULT_TOPIC,
            "2": "演示重复搜索请求如何被程序拦截",
            "3": "演示没有外部证据时如何说明不足",
        }.get(selected, DEFAULT_TOPIC)
        if selected == "4":
            topic = ask("请输入问题：") or DEFAULT_TOPIC
        if searches == 0:
            # The fixed model can read a registered source without a search.
            args += ["--source-url", "https://example.org/guide"]
            print("0搜索演示：登记 example.org/guide 并返回固定正文，不访问该网址。")
            scenario = "normal"
        args += ["--scenario", scenario]
    else:
        args += ["--live", "--model-retries", "0", "--model-max-tokens", str(tokens)]
        if searches:
            provider = choice(
                "公共搜索服务",
                {
                    "1": "auto：有 Tavily key 则用 Tavily，否则 DuckDuckGo",
                    "2": "DuckDuckGo",
                    "3": "Tavily（需要本机 key）",
                },
                "1",
            )
            args += ["--search-provider", {"1": "auto", "2": "duckduckgo", "3": "tavily"}[provider]]
        preset = (
            choice(
                "问题",
                {
                    "1": "预设：读取 Python 官方 asyncio.wait 文档",
                    "2": "输入自己的问题与可选来源 URL",
                },
                "1" if pages else "2",
            )
            if pages
            else "2"
        )
        topic = LIVE_TOPIC if preset == "1" else (ask("请输入研究问题：") or DEFAULT_TOPIC)
        urls = [LIVE_URL] if preset == "1" else []
        if pages and preset == "2":
            while True:
                raw = ask("指定来源 URL（最多3个，空格分隔；0搜索时必填）：")
                try:
                    urls = normalize_source_urls(raw.split())
                    if not urls and searches == 0:
                        raise ValueError("0搜索需要至少一个来源 URL")
                    break
                except ValueError as exc:
                    print(terminal_text(exc))
        for url in urls:
            args += ["--source-url", url]
    # Use -- to keep a question beginning with '-' a positional argument.
    return [*args, "--", topic]


def show_answer(run: dict, folder: Path) -> None:
    metrics = run["metrics"]
    mode = run.get("mode", "unknown")
    label = {
        "offline_fixture": "离线固定演示：回答不代表对任意问题的真实研究",
        "live": "真实模型运行：质量仍需核对保存证据",
        "live_model_fixture": "真实模型配固定工具证据",
    }.get(mode, f"历史/其他模式：{mode}")
    print("\n" + "=" * 60)
    print(terminal_text(label))
    print("问题：" + terminal_text(run.get("topic", "未记录")))
    print(
        f"状态：{terminal_text(metrics.get('status'))} / {terminal_text(metrics.get('stop_reason'))}"
    )
    if metrics.get("status") == "completed":
        print("研究流程已结束；是否有证据或长度警告，请看下方提示。")
        if metrics.get("stop_reason") in {"search_budget", "duplicate_query", "no_new_evidence"}:
            print("上述停止原因指搜索阶段结束，不等于整个任务失败；之后仍可读页和回答。")
    else:
        print("本次研究未完整完成；下方可能是部分回答或程序提示，请核对 trace。")
    print("\n保存的回答：\n" + terminal_text(run["answer"]))
    print("\n程序计数（包含失败尝试）：")
    summary = execution_summary(run)
    counts = summary["counts"]
    print(
        f"工具请求 {counts['tool_invocations']}；搜索尝试 {counts['search_attempts']}；读页尝试 {counts['page_read_attempts']}；保存正文 {counts['pages_read']}"
    )
    print(
        terminal_text(
            f"回答非空白字符：{metrics.get('answer_chars', '未记录')}；strict 业务退出码：{result_exit_code(run, True)}"
        )
    )
    for warning in [*metrics.get("warnings", []), *summary["issues"]]:
        print("提示：" + terminal_text(warning))
    print("\n来源目录（是否读过正文需看 @page / page_evidence）：")
    for source in run.get("sources", []):
        print(terminal_text(f"  {source.get('id')}  {source.get('title')}\n  {source.get('url')}"))
    print("\n本次目录：" + terminal_text(folder.resolve()))


def show_trace(run: dict) -> None:
    print("\n应用工具 trace（不是私密思维链）：")
    for index, event in enumerate(run.get("trace", []), 1):
        print(
            terminal_text(
                f"  {index}. {event.get('tool')} | {event.get('status')} | 后端尝试={event.get('backend_attempted', '未记录')}"
            )
        )
        print(terminal_text(json.dumps(event, ensure_ascii=False, indent=2)))
    if not run.get("trace"):
        print("  没有应用工具记录；可能尚未调用工具，或调用在 SDK 层被拒绝。")
    for key in ("model_requests", "provider_responses", "mcp"):
        if run.get(key):
            print(terminal_text(key + "：\n" + json.dumps(run[key], ensure_ascii=False, indent=2)))


def viewer_environment() -> dict[str, str]:
    """Give desktop viewers OS/session settings, not model keys or editor hooks."""
    return {key: value for key, value in os.environ.items() if key.upper() in VIEWER_ENV_NAMES}


def open_local(path: Path) -> None:
    """Submit an associated-app request away from the menu's console and stdio.

    Windows ShellExecute may start an editor that attaches to its parent's
    console. Run it in a short-lived, detached pythonw helper, not in this menu.
    python.exe in a Windows venv is a redirector and may allocate another console.
    Only wait for submission (at most 10s), never for the editor to close.
    """
    path = path.resolve(strict=True)
    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "shell": False,
        "check": True,
        "timeout": 10,
        "env": viewer_environment(),
        "cwd": path if path.is_dir() else path.parent,
    }
    if sys.platform == "win32":
        command = [
            str(Path(sys.executable).with_name("pythonw.exe")),
            "-I",
            "-S",
            "-c",
            "import os,sys; os.startfile(sys.argv[1])",
            str(path),
        ]
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        command = ["open" if sys.platform == "darwin" else "xdg-open", str(path)]
        options["start_new_session"] = True
    subprocess.run(command, **options)


def inspect_run(run: dict, folder: Path) -> None:
    show_answer(run, folder)
    while True:
        selected = choice(
            "查看本次结果（不会重新运行模型）",
            {
                "1": "再次查看回答",
                "2": "在终端查看完整 trace / 模型请求摘要",
                "3": "用默认程序打开 report.md",
                "4": "用默认程序打开 run.json",
                "5": "打开本次文件夹",
                "6": "在终端直接查看 run.json（不启动外部程序）",
                "0": "返回主页",
            },
            "0",
        )
        if selected == "0":
            return
        if selected == "1":
            show_answer(run, folder)
        elif selected == "2":
            show_trace(run)
        elif selected == "6":
            try:
                saved = load_saved_run(folder / "run.json")
                print("\n保存的 run.json（只读，未重新运行）：")
                print(terminal_text(json.dumps(saved, ensure_ascii=False, indent=2)))
            except (OSError, ValueError, RecursionError):
                print(
                    "无法读取有效的运行记录，请检查文件："
                    + terminal_text((folder / "run.json").resolve())
                )
        else:
            path = (
                folder / {"3": "report.md", "4": "run.json"}[selected]
                if selected != "5"
                else folder
            )
            try:
                open_local(path)
                print("已提交默认程序打开请求；可继续使用本菜单。查看run.json也可选6。")
            except (OSError, subprocess.SubprocessError):
                print(
                    "未能确认默认程序已接收请求；不会自动重试。可选6查看run.json，或复制路径手动打开："
                    + terminal_text(path.resolve())
                )


def load_saved_run(path: Path) -> dict:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("run.json 超过20MiB，请用编辑器查看")
    run = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(run, dict) or not isinstance(run.get("answer"), str):
        raise ValueError("不是包含回答的研究运行记录")
    if not isinstance(run.get("topic", ""), str) or not isinstance(run.get("mode", "unknown"), str):
        raise ValueError("问题/模式格式不正确")
    metrics = run.get("metrics")
    if (
        not isinstance(metrics, dict)
        or not {"status", "stop_reason", "warnings", "unique_sources"} <= metrics.keys()
    ):
        raise ValueError("缺少必要的运行指标")
    if not isinstance(metrics["warnings"], list) or not isinstance(run.get("sources", []), list):
        raise ValueError("指标/来源格式不正确")
    if (
        not all(isinstance(w, str) for w in metrics["warnings"])
        or not isinstance(metrics["status"], str)
        or not isinstance(metrics["stop_reason"], str)
        or type(metrics["unique_sources"]) is not int
        or metrics["unique_sources"] < 0
    ):
        raise ValueError("运行指标字段格式不正确")
    if not all(isinstance(s, dict) for s in run.get("sources", [])):
        raise ValueError("来源格式不正确")
    if not isinstance(run.get("trace", []), list) or not all(
        isinstance(e, dict) for e in run.get("trace", [])
    ):
        raise ValueError("trace 格式不正确")
    return run


def browse_history(output_dir: Path) -> None:
    """Read recent records recursively, including nested verification runs."""
    if not output_dir.is_dir():
        print("还没有运行记录；先完成一次离线演示。")
        return
    paths = sorted(output_dir.rglob("run.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    records = []
    skipped = 0
    for path in paths:
        try:
            run = load_saved_run(path)
        except (OSError, ValueError, RecursionError):
            skipped += 1
            continue
        records.append((path, run))
        if len(records) == 20:
            break
    if skipped:
        print(f"跳过 {skipped} 个不可读/格式不完整的记录；没有修改原件。")
    if not records:
        print("没有可查看的研究记录。")
        return
    options = {
        str(i): terminal_text(
            f"{run.get('mode', 'unknown')} | {run.get('topic', '')[:60]} | {path.parent.relative_to(output_dir)}"
        )
        for i, (path, run) in enumerate(records, 1)
    }
    selected = choice(
        "最近20次以内的记录（按文件修改时间；只读查看）", options | {"0": "返回主页"}, "0"
    )
    if selected != "0":
        path, run = records[int(selected) - 1]
        inspect_run(run, path.parent)


def run_terminal(output_dir: Path = Path("reports")) -> int:
    print(
        "\nAgentScope 研究助手 · 终端交互\n回车采用方括号内的默认值；默认离线。q 返回主页，Ctrl+C / Ctrl+Z 后回车退出。"
    )
    last_code = 0
    try:
        while True:
            try:
                selected = choice(
                    "主页",
                    {
                        "1": "离线固定演示（无需 key，不收费）",
                        "2": "真实模型研究（需要 .env 和网络，可能计费）",
                        "3": "查看历史回答 / trace / 文件",
                        "4": "评测中心：固定 / 自定义测试 / 成绩与改进",
                        "0": "退出",
                    },
                    "1",
                )
                if selected == "0":
                    return last_code
                if selected == "3":
                    browse_history(output_dir)
                    continue
                if selected == "4":
                    from .evaluation_terminal import run_evaluation_center

                    evaluation_code = run_evaluation_center()
                    if evaluation_code is not None:
                        last_code = evaluation_code
                    continue
                live = selected == "2"
                args = collect_task(live, output_dir)
                parser = create_parser()
                task = parser.parse_args(args)
                print(
                    f"\n本题设置：{'真实模型' if live else '离线固定演示'} / {task.transport} / {task.reader}"
                )
                print(
                    f"搜索≤{task.searches}次；读页≤{task.pages}次；主循环≤{task.timeout:g}秒、{task.max_agent_iters}轮；回答≤{task.max_answer_chars}非空白字符。"
                )
                if live:
                    print(
                        f"每次模型输出≤{task.model_max_tokens} token；指定来源{len(task.source_url)}个。字符预算与token预算不是同一个限制。"
                    )
                print(
                    "\n即将执行的等价命令（PowerShell）：\nuv run --offline --no-sync as-research "
                    + " ".join("'" + arg.replace("'", "''") + "'" for arg in args)
                )
                print("零自动重试；自动缩稿关闭；每次独立保存 run.json 与 report.md。")
                if live:
                    if (
                        ask("输入 LIVE 才开始真实请求（可能计费）；回车返回主页：").casefold()
                        != "live"
                    ):
                        continue
                elif choice("开始离线演示？", {"1": "开始", "0": "返回主页"}, "1") == "0":
                    continue
                print("\n正在运行，请等待；Ctrl+C 中断。保存前中断可能没有本次记录。", flush=True)
                try:
                    run, folder = execute_args(task, parser)
                except SystemExit as exc:
                    last_code = int(exc.code or 0)
                    print("本次未完成：请按上方配置/安装提示修正；不会自动重试。")
                    continue
                last_code = result_exit_code(run, True)
                inspect_run(run, folder)
            except BackToMenu:
                print("已返回主页。")
            except OSError:
                last_code = 2
                print("本地文件访问失败，请检查输出目录与权限。")
    except EOFError:
        print("\n终端会话已退出；已保存的记录保留。")
        return last_code
    except KeyboardInterrupt:
        print("\n终端会话已退出；已保存的记录保留。")
        return 130
