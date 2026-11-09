"""Bounded public HTML reads, optionally extracted using an isolated browser DOM.

The browser renders downloaded HTML with JavaScript and all network disabled.
It does not control the user's browser, log in, click buttons or execute page JS.
"""

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from .core import canonical_url
from .segments import parse_document, select_passages

PAGE_READ_ERROR_CODES = frozenset(
    {
        "invalid_url",
        "port_blocked",
        "private_address_blocked",
        "redirect_blocked",
        "unsupported_content_type",
        "page_too_large",
        "empty_page",
        "excerpt_budget_insufficient",
        "invalid_page_payload",
        "invalid_page_metadata",
        "page_reader_error",
        "page_timeout",
        "page_connection_error",
        "page_http_error",
        "page_transport_error",
        "empty_page_excerpt",
        "page_budget_exceeded",
    }
)


class PageReadError(ValueError):
    """A public, non-secret error code suitable for a tool response."""


def page_error_code(exc: Exception) -> str:
    """Same safe category for local readers and MCP server responses."""
    if isinstance(exc, PageReadError) and str(exc) in PAGE_READ_ERROR_CODES:
        return str(exc)
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "page_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return "page_http_error"
    if isinstance(exc, (httpx.RequestError, OSError)):
        return "page_connection_error"
    return "page_reader_error"


async def validate_public_url(url: str) -> str:
    cleaned = canonical_url(url)
    if cleaned is None:
        raise PageReadError("invalid_url")
    parts = urlsplit(cleaned)
    if parts.port not in {None, 80, 443}:
        raise PageReadError("port_blocked")
    addresses = await asyncio.to_thread(
        socket.getaddrinfo,
        parts.hostname,
        parts.port or (443 if parts.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if not addresses or any(
        not ipaddress.ip_address(a[4][0].split("%")[0]).is_global for a in addresses
    ):
        raise PageReadError("private_address_blocked")
    return cleaned


async def fetch_html(url: str, *, transport=None, validator=validate_public_url) -> str:
    url = await validator(url)
    # trust_env=False prevents inherited proxy credentials being sent or proxy
    # routing bypassing our destination checks. DNS rebinding still needs an
    # egress proxy/pinned-IP transport for production-grade protection.
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=False, trust_env=False, transport=transport
    ) as client:
        async with client.stream(
            "GET", url, headers={"User-Agent": "EvidenceResearcher/0.2"}
        ) as response:
            if 300 <= response.status_code < 400:
                raise PageReadError("redirect_blocked")
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";")[0].lower() not in {
                "text/html",
                "application/xhtml+xml",
            }:
                raise PageReadError("unsupported_content_type")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 1_000_000:
                    raise PageReadError("page_too_large")
            return body.decode(response.encoding or "utf-8", errors="replace")


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "div", "section", "li", "h1", "h2", "h3", "pre", "br"}:
            self.parts.append("\n")
        if tag in {"script", "style", "noscript", "nav", "footer", "header", "template"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"p", "div", "section", "li", "h1", "h2", "h3", "pre"}:
            self.parts.append("\n")
        if tag in {"script", "style", "noscript", "nav", "footer", "header", "template"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


async def browser_text(html: str, *, channel: str = "msedge", return_html: bool = False) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=channel or None, headless=True)
        try:
            context = await browser.new_context(
                java_script_enabled=False, service_workers="block", accept_downloads=False
            )
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            await page.set_content(html, wait_until="domcontentloaded", timeout=10_000)
            await page.locator(
                "script,style,noscript,nav,footer,header,iframe,template"
            ).evaluate_all("nodes => nodes.forEach(n => n.remove())")
            if return_html:
                return await page.locator("body").inner_html(timeout=5_000)
            return await page.locator("body").inner_text(timeout=5_000)
        finally:
            await browser.close()


def select_excerpt(text: str, focus: str, limit: int) -> str:
    """Return bounded windows around up to three literal terms, not just a page's header."""
    terms = [term.strip() for term in re.split(r"[,，;；]", focus) if term.strip()][:3]
    windows = []
    width = max(100, (limit - 150) // max(1, len(terms) * 2))
    for term in terms:
        # Match original text so Unicode case conversions cannot shift offsets.
        offsets = [m.start() for m in re.finditer(re.escape(term), text, re.IGNORECASE)][:2]
        for offset in offsets:
            start = max(0, offset - min(120, width // 4))
            end = min(len(text), start + width)
            windows.append((start, end))
    if not windows:
        return text[:limit]
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return "\n\n[excerpt gap]\n\n".join(text[a:b] for a, b in merged)[:limit]


class PageReader:
    def __init__(self, mode: str = "http", channel: str = "msedge", strategy: str = "segments"):
        if mode not in {"http", "browser"}:
            raise ValueError("Reader must be http or browser")
        self.mode, self.channel = mode, channel
        if strategy not in {"head", "window", "segments", "bm25", "balanced"}:
            raise ValueError("Unknown excerpt strategy")
        self.strategy = strategy

    async def __call__(self, url: str, max_chars: int, focus: str = "") -> dict:
        html = await fetch_html(url)
        if self.strategy in {"segments", "bm25", "balanced"}:
            if self.mode == "browser":
                html = await browser_text(html, channel=self.channel, return_html=True)
            document = parse_document(html)
            if not document["text"]:
                raise PageReadError("empty_page")
            result = select_passages(
                document,
                focus,
                max_chars,
                ranking="lexical" if self.strategy == "segments" else self.strategy,
            )
            if not result["content"]:
                raise PageReadError("excerpt_budget_insufficient")
            return result
        if self.mode == "browser":
            text = await browser_text(html, channel=self.channel)
        else:
            parser = TextExtractor()
            parser.feed(html)
            text = "".join(parser.parts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        if not text:
            raise PageReadError("empty_page")
        return {
            "content": text[:max_chars]
            if self.strategy == "head"
            else select_excerpt(text, focus, max_chars),
            "strategy": self.strategy,
            "is_full_document": False,
        }


class FixtureReader:
    mode = "fixture"

    async def __call__(self, url: str, max_chars: int, focus: str = "") -> str:
        return "[Synthetic page fixture] Retrieval relevance and factual support are different metrics."[
            :max_chars
        ]
