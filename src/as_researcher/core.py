"""Framework-independent budgets, evidence normalization and citation diagnostics.

Adapted from the local MIT-licensed Local Deep Researcher interview edition.
"""

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evidence import citation_ids


class ResearchConfig(BaseModel):
    """Keep resource policy outside the model's control."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    max_searches: int = Field(default=3, ge=0, le=10)
    results_per_search: int = Field(default=3, ge=1, le=5)
    chars_per_source: int = Field(default=1500, ge=100, le=4000)
    max_agent_iters: int = Field(default=10, ge=2, le=30)
    search_timeout: float = Field(default=20, gt=0, le=60)
    total_timeout: float = Field(default=120, gt=0, le=600)
    early_stop_duplicates: bool = True
    max_page_reads: int = Field(default=2, ge=0, le=5)
    chars_per_page: int = Field(default=5000, ge=100, le=12000)
    page_timeout: float = Field(default=30, gt=0, le=60)
    max_answer_chars: int | None = Field(default=None, ge=100, le=10000)
    answer_style: Literal["standard", "compact"] = "standard"
    revise_answer: bool = False
    revision_timeout: float = Field(default=30, gt=0, le=60)
    revision_max_tokens: int = Field(default=1200, ge=64, le=4096)
    revision_thinking: Literal["provider-default", "disabled"] = "provider-default"

    @model_validator(mode="after")
    def validate_revision(self):
        if self.revise_answer and self.max_answer_chars is None:
            raise ValueError("revise_answer requires max_answer_chars")
        if self.revision_thinking != "provider-default" and not self.revise_answer:
            raise ValueError("revision_thinking requires revise_answer")
        return self


def normalize_query(query: str) -> str:
    return " ".join(query.casefold().split())


def canonical_url(url: str) -> str | None:
    """Normalize source identity. This is link validation, NOT DNS/SSRF protection.

    Optional page reading must additionally pass pages.validate_public_url.
    """
    try:
        if re.search(r"[\s\\<>]", url):
            return None
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in {"http", "https"} or not host:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host:
                return None
        port = parts.port  # Also validates malformed/out-of-range ports.
        authority = f"[{host}]" if ":" in host else host
        if port is not None and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
            authority += f":{port}"
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid"}
            ]
        )
        return urlunsplit((parts.scheme, authority, parts.path or "/", query, ""))
    except (ValueError, TypeError):
        return None


def source_id(url: str) -> str:
    return "SRC-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:8].upper()


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    content: str

    @property
    def id(self) -> str:
        return source_id(self.url)


def citation_diagnostics(text: str, sources: list[Source]) -> dict:
    known = {item.id for item in sources}
    # Catch malformed as well as well-formed invented IDs. Count before adding
    # the generated source list so the bibliography cannot inflate this metric.
    used = set(citation_ids(text))
    valid = used & known
    return {
        "used_ids": sorted(used),
        "valid_ids": sorted(valid),
        "invalid_ids": sorted(used - known),
        "id_validity_rate": len(valid) / len(used) if used else None,
        "has_citations": bool(used),
        "note": "ID existence only; not factual accuracy or claim support.",
    }
