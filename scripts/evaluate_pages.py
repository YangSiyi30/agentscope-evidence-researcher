"""Predeclared public-page retrieval experiments, no LLM calls or answer scores."""

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from as_researcher import segments
from as_researcher.pages import TextExtractor, fetch_html, select_excerpt


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalized(value: str) -> str:
    return " ".join(value.split()).casefold()


def target_checks(content: str, targets: list[str]) -> dict:
    text = normalized(content)
    needles = [normalized(t) for t in targets]
    positions = [[m.start() for m in re.finditer(re.escape(t), text)] for t in needles]
    all_present = all(positions)
    # Literal co-occurrence is deliberately weaker than table/definition semantics.
    nearby = all_present and any(
        all(
            any(start <= p and p + len(term) <= start + 500 for p in hits)
            for term, hits in zip(needles, positions, strict=True)
        )
        for hits in positions
        for start in hits
    )
    return {"all_targets_hit": all_present, "targets_within_500_chars": bool(nearby)}


def compare(case: dict, html: str, budgets: list[int]) -> list[dict]:
    extractor = TextExtractor()
    extractor.feed(html)
    raw_text = "".join(extractor.parts)
    text = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", raw_text)).strip()
    target_in_page = target_checks(text, case["targets"])["all_targets_hit"]
    document = segments.parse_document(html)
    rows = []
    for budget in budgets:
        selected = segments.select_passages(document, case["focus"], budget)
        outputs = {
            "head": text[:budget],
            "window": select_excerpt(text, case["focus"], budget),
            "segments": selected["content"],
        }
        for strategy, content in outputs.items():
            checks = (
                target_checks(content, case["targets"])
                if target_in_page
                else {"all_targets_hit": None, "targets_within_500_chars": None}
            )
            rows.append(
                {
                    "case": case["id"],
                    "strategy": strategy,
                    "budget": budget,
                    "chars": len(content),
                    "within_budget": len(content) <= budget,
                    "target_in_page": target_in_page,
                    **checks,
                    "content": content,
                    "selected": selected if strategy == "segments" else None,
                }
            )
    return rows


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, default=Path("eval/passages_v05.json"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--download", action="store_true", help="Download only the declared pages (at most five)"
    )
    group.add_argument("--snapshots", type=Path, help="Replay snapshots without network")
    parser.add_argument("--label", choices=["baseline", "development"], default="baseline")
    args = parser.parse_args()
    plan_bytes = args.plan.read_bytes()
    plan = json.loads(plan_bytes)
    if not args.download and args.snapshots is None:
        print(
            json.dumps(
                {"mode": "plan_only", "plan_sha256": digest(plan_bytes), **plan},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if any(not 100 <= b <= 12000 for b in plan["budgets"]) or len(plan["cases"]) > 5:
        parser.error("At most five pages; budgets 100..12000")
    root = Path("reports") / (
        "pages-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    )
    root.mkdir(parents=True)
    (root / "plan.json").write_bytes(plan_bytes)
    (root / "segments_source.py").write_bytes(Path(segments.__file__).read_bytes())
    snapshots = args.snapshots or root / "snapshots"
    pages, errors = {}, []
    if args.download:
        snapshots.mkdir()
        manifest = {"plan_sha256": digest(plan_bytes), "pages": {}}
        for case in plan["cases"]:
            try:
                html = await asyncio.wait_for(fetch_html(case["url"]), timeout=30)
                (snapshots / (case["id"] + ".html")).write_bytes(html.encode("utf-8"))
                manifest["pages"][case["id"]] = {
                    "url": case["url"],
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "sha256": digest(html.encode("utf-8")),
                }
                pages[case["id"]] = html
            except Exception as exc:
                errors.append({"case": case["id"], "error_type": type(exc).__name__})
        (snapshots / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    else:
        manifest = json.loads((snapshots / "manifest.json").read_text(encoding="utf-8"))
        if manifest["plan_sha256"] != digest(plan_bytes):
            raise ValueError("Plan fingerprint mismatch")
        for case in plan["cases"]:
            saved = manifest["pages"].get(case["id"])
            if saved is None:
                errors.append({"case": case["id"], "error_type": "snapshot_unavailable"})
                continue
            raw = (snapshots / (case["id"] + ".html")).read_bytes()
            if digest(raw) != saved["sha256"] or saved["url"] != case["url"]:
                raise ValueError("Snapshot fingerprint or URL mismatch")
            pages[case["id"]] = raw.decode("utf-8")
    rows = [
        row
        for case in plan["cases"]
        if case["id"] in pages
        for row in compare(case, pages[case["id"]], plan["budgets"])
    ]
    result = {
        "label": args.label,
        "note": plan["purpose"],
        "plan_sha256": digest(plan_bytes),
        "selector_source_sha256": digest(Path(segments.__file__).read_bytes()),
        "snapshots": str(snapshots),
        "manifest": manifest,
        "errors": errors,
        "rows": rows,
    }
    (root / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 固定目标页面定位验证",
        "",
        plan["metric"],
        "",
        "| 样例 | 方法 | 预算 | 字符 | 页面含目标 | 命中 | 500 字符内共同出现 |",
        "|---|---|---:|---:|---|---|---|",
    ]
    lines += [
        f"| {r['case']} | {r['strategy']} | {r['budget']} | {r['chars']} | {r['target_in_page']} | {r['all_targets_hit']} | {r['targets_within_500_chars']} |"
        for r in rows
    ]
    (root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(root),
                "label": args.label,
                "pages": len(pages),
                "errors": errors,
                "comparisons": len(rows),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
