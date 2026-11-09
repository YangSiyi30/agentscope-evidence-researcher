"""Compare three selectors on identical saved HTML; target-span hits are not answer scores."""

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from as_researcher.pages import TextExtractor, fetch_html, select_excerpt
from as_researcher.segments import parse_document, select_passages

URLS = {
    "python": "https://docs.python.org/3/library/asyncio-task.html",
    "mcp": "https://modelcontextprotocol.io/specification/2025-11-25/basic/transports",
}
CASES = [
    ("python_timeout", "python", "asyncio.timeout", "cancelling the current task"),
    ("python_wait_for", "python", "wait_for", "wait until the future is actually cancelled"),
    ("mcp_process", "mcp", "stdio", "launches the MCP server as a subprocess"),
    ("mcp_stderr", "mcp", "stderr", "SHOULD NOT assume stderr output indicates error conditions"),
]


def compare(name, html, focus, target, budget, source_kind):
    parser = TextExtractor()
    parser.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    document = parse_document(html)
    outputs = {
        "head": text[:budget],
        "window": select_excerpt(text, focus, budget),
        "segments": select_passages(document, focus, budget)["content"],
    }

    def normalized(value):
        return " ".join(value.split()).casefold()

    present = normalized(target) in normalized(text)
    return [
        {
            "case": name,
            "source_kind": source_kind,
            "strategy": strategy,
            "budget": budget,
            "chars": len(content),
            "target_in_page": present,
            "target_span_hit": normalized(target) in normalized(content) if present else None,
            "content": content,
        }
        for strategy, content in outputs.items()
    ]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--download", action="store_true", help="Fetch two public pages; never calls a model"
    )
    parser.add_argument("--snapshots", type=Path, help="Replay an existing snapshot directory")
    parser.add_argument("--budget", type=int, default=800)
    args = parser.parse_args()
    if not 100 <= args.budget <= 12000 or (args.download and args.snapshots):
        parser.error("Budget must be 100..12000; choose download or snapshots, not both")
    root = Path("reports") / ("passage-ablation-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S"))
    root.mkdir(parents=True, exist_ok=False)
    snapshots = args.snapshots or root / "snapshots"
    manifest, errors, pages = {}, [], {}
    if args.download:
        snapshots.mkdir()
        for name, url in URLS.items():
            try:
                html = await asyncio.wait_for(fetch_html(url), timeout=30)
                (snapshots / f"{name}.html").write_text(html, encoding="utf-8")
                manifest[name] = {
                    "url": url,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "sha256": hashlib.sha256(html.encode()).hexdigest(),
                }
                pages[name] = html
            except Exception as exc:
                errors.append({"page": name, "error_type": type(exc).__name__})
        (snapshots / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    elif args.snapshots:
        manifest = json.loads((snapshots / "manifest.json").read_text(encoding="utf-8"))
        for name in URLS:
            if name not in manifest:
                continue
            html = (snapshots / f"{name}.html").read_text(encoding="utf-8")
            if hashlib.sha256(html.encode()).hexdigest() != manifest[name]["sha256"]:
                raise ValueError("Snapshot fingerprint mismatch")
            pages[name] = html
    rows = []
    for name, page, focus, target in CASES:
        if page in pages:
            rows.extend(compare(name, pages[page], focus, target, args.budget, "real_page"))
    synthetic = (
        '<div class="toc">'
        + '<p><a href="#x">timeout</a></p>' * 60
        + "</div><p>"
        + "Background. " * 250
        + "</p><h2>timeout</h2><p>The current task receives cancellation at its deadline.</p>"
    )
    rows.extend(
        compare(
            "late_definition_with_toc",
            synthetic,
            "timeout",
            "receives cancellation",
            args.budget,
            "synthetic",
        )
    )
    rows.extend(
        compare(
            "no_keyword_fallback",
            "<p>Opening evidence is here.</p><p>Other details.</p>",
            "missing",
            "Opening evidence",
            args.budget,
            "synthetic",
        )
    )
    result = {
        "note": "Predeclared substring targets, NOT factual support or model-quality scores. Small handpicked sample. Compare within source kind.",
        "snapshots": str(snapshots),
        "manifest": manifest,
        "errors": errors,
        "rows": rows,
    }
    (root / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 正文定位对照",
        "",
        result["note"],
        "",
        "| 样例 | 来源 | 方法 | 字符 | 目标片段命中 |",
        "|---|---|---|---:|---|",
    ]
    lines += [
        f"| {r['case']} | {r['source_kind']} | {r['strategy']} | {r['chars']} | {r['target_span_hit']} |"
        for r in rows
    ]
    (root / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {"output": str(root), "errors": errors, "comparisons": len(rows)}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
