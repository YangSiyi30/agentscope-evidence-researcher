"""Inspect or export revision comparisons without model calls or label mutations."""

import argparse
import json
from pathlib import Path

from as_researcher.comparison import check_comparison, prepare_comparison, render_comparison


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "check", "export"])
    parser.add_argument("source_run", type=Path)
    parser.add_argument("revision", type=Path)
    parser.add_argument("packet", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    run = json.loads(args.source_run.read_text("utf-8"))
    revision = json.loads(args.revision.read_text("utf-8"))
    if args.action == "prepare":
        with args.packet.open("x", encoding="utf-8") as file:
            json.dump(prepare_comparison(run, revision), file, ensure_ascii=False, indent=2)
        print("Saved unassigned comparison draft:", args.packet)
        return 0
    packet = json.loads(args.packet.read_text("utf-8"))
    check = check_comparison(run, revision, packet)
    if args.action == "export":
        if args.output is None:
            parser.error("export requires --output (new HTML file)")
        content = render_comparison(run, revision, packet)
        with args.output.open("x", encoding="utf-8") as file:
            file.write(content)
        print("Saved read-only comparison:", args.output)
    else:
        print(json.dumps(check, ensure_ascii=False, indent=2))
    return 0 if check["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
