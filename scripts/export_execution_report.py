"""Export a separate versioned report view; never overwrite a saved run/report."""

import argparse
import hashlib
import json
from pathlib import Path

from as_researcher.researcher import render_report


def export(source: Path, output: Path):
    raw = source.read_bytes()
    run = json.loads(raw)
    view = {**run, "report_renderer_version": 3}
    header = (
        "> 这是已有运行的另存展示，没有重新调用模型或工具，原回答与原运行文件未改写。\n"
        f"> 原始run文件SHA256：{hashlib.sha256(raw).hexdigest()}\n\n"
    )
    body = header + render_report(view)
    with output.open("x", encoding="utf-8") as file:
        file.write(body)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        export(args.run, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error("Cannot export report: " + type(exc).__name__)
    print(f"Saved separate execution report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
