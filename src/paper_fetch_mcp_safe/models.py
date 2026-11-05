"""Stable, transport-independent objects returned by the MCP tools."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

LOGGER = logging.getLogger("paper_fetch_mcp_safe")
MESSAGE_LIMIT = 120
_URL_RE = re.compile(r"https?://\S+")
_SPACE_RE = re.compile(r"\s+")
_HTTP_STATUS_RE = re.compile(r"(?:Client|Server) error '(\d{3}[^']*)'.*")
_HTTP_NOISE_RE = re.compile(r"(for url|For more information check:?)\s*", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(r"([?&](?:api[_-]?key|apikey|access[_-]?token|token|password|secret)=)[^&\s'\"]+",
                              re.IGNORECASE)


def short_message(message: str) -> str:
    """One-line, URL-free provider message for tool responses."""
    text = _SPACE_RE.sub(" ", _URL_RE.sub("", message)).strip()
    text = _HTTP_STATUS_RE.sub(r"HTTP \1", text)
    text = _SPACE_RE.sub(" ", _HTTP_NOISE_RE.sub("", text)).strip(" :;,.'\"")
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 1].rstrip() + "…"
    return text or "see server log"


def redact_secrets(message: str) -> str:
    """Drop credential-bearing query parameters so log output stays safe to share."""
    return _SECRET_QUERY_RE.sub(r"\1REDACTED", message)


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PAYWALL_NO_OA = "PAYWALL_NO_OA"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    BAD_IDENTIFIER = "BAD_IDENTIFIER"
    DOWNLOAD_CORRUPT = "DOWNLOAD_CORRUPT"
    LICENSE_RESTRICTED = "LICENSE_RESTRICTED"


@dataclass(slots=True)
class PdfCandidate:
    url: str
    provider: str
    license: str | None = None
    version: str | None = None
    compliance_tier: str = "oa"
    oa_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PaperRecord:
    title: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    arxiv_id: str | None = None
    year: int | None = None
    oa_status: str | None = None
    pdf_candidates: list[PdfCandidate] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)

    def best_identifier(self) -> str:
        for kind in ("pmcid", "arxiv_id", "doi", "pmid"):
            value = getattr(self, kind)
            if value:
                return f"{'arxiv' if kind == 'arxiv_id' else kind}:{value}"
        for kind in ("openreview_id", "acl_id", "pmlr_id", "osf_id", "zenodo_id", "hal_id"):
            if value := self.extra.get(kind):
                return f"{kind.removesuffix('_id')}:{value}"
        return f"title:{self.title or 'unknown'}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FetchResult:
    ok: bool
    identifier: str
    path: str | None = None
    sha256: str | None = None
    provider: str | None = None
    compliance_tier: str | None = None
    license: str | None = None
    license_class: str | None = None
    license_evidence: str | None = None
    warning: str | None = None
    skipped: bool = False
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FetchError(Exception):
    def __init__(self, code: ErrorCode, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        original = str(self)
        full = redact_secrets(original)
        message = short_message(full)
        if message != original:
            # The response always carries the short form; only a lossy message needs the log.
            LOGGER.warning("%s: %s", self.code.value, full)
        result: dict[str, Any] = {"code": self.code.value, "message": message}
        if self.retry_after is not None:
            result["retry_after"] = max(0, int(self.retry_after + 0.999))
        return result
