"""Bounded user-supplied URLs are source locations, never retrieved evidence."""

from urllib.parse import urlsplit

from .core import canonical_url

SOURCE_CONTEXT_MARKER = "\n\n应用登记的初始来源（URL本身不是证据）：\n"


def normalize_source_urls(urls: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Validate before model setup; actual reads still require public DNS checks."""
    if not isinstance(urls, (list, tuple)) or len(urls) > 3:
        raise ValueError("source_urls must be a list/tuple of at most 3 URLs")
    normalized = []
    for value in urls:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise ValueError("Each source URL must be a nonempty string up to 2048 characters")
        url = canonical_url(value)
        if url is None or urlsplit(url).port not in (None, 80, 443):
            raise ValueError("Source URLs must be public HTTP(S) links on standard ports")
        if url not in normalized:
            normalized.append(url)
    return tuple(normalized)
