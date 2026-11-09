"""Prepare an editable evidence-review packet, or validate one without API calls."""

import argparse
import json
from pathlib import Path

from as_researcher.atomic_review import prepare_atomic_review
from as_researcher.review import check_review, prepare_review
from as_researcher.worksheet import render_worksheet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "prepare-atomic", "check", "worksheet"])
    parser.add_argument("run", type=Path)
    parser.add_argument("packet", type=Path)
    parser.add_argument(
        "--output", type=Path, help="New Markdown file for worksheet; never overwritten"
    )
    args = parser.parse_args()
    run = json.loads(args.run.read_text(encoding="utf-8"))
    if args.action == "worksheet":
        if args.output is None:
            parser.error("worksheet requires --output")
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        content = render_worksheet(run, packet)
        with args.output.open("x", encoding="utf-8") as file:
            file.write(content)
        print(f"Saved read-only worksheet: {args.output}")
    elif args.action != "check":
        packet = (
            prepare_atomic_review(run) if args.action == "prepare-atomic" else prepare_review(run)
        )
        with args.packet.open("x", encoding="utf-8") as file:
            json.dump(packet, file, ensure_ascii=False, indent=2)
        print(f"Saved {len(packet['units'])} draft units; all pending: {args.packet}")
    else:
        result = check_review(run, json.loads(args.packet.read_text(encoding="utf-8")))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["valid"] else 1)


if __name__ == "__main__":
    main()
