"""Provider protocol and response normalization helpers."""

from __future__ import annotations

from typing import Protocol

from ..models import ErrorCode, FetchError, PaperRecord, PdfCandidate


class ProviderError(FetchError):
    def __init__(self, provider: str, message: str, code: ErrorCode = ErrorCode.PROVIDER_UNAVAILABLE, retry_after: float | None = None):
        super().__init__(code, f"{provider}: {message}", retry_after)
        self.provider = provider


class Provider(Protocol):
    name: str
    priority: int
    compliance_tier: str

    def supports(self, record: PaperRecord) -> bool: ...

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]: ...


def normalized_version(value: str | None) -> str | None:
    if not value:
        return None
    lower = value.lower()
    if "publish" in lower or lower in {"vor", "version of record"}:
        return "vor"
    if "accept" in lower or lower in {"am", "author manuscript"}:
        return "am"
    if "submit" in lower or lower == "preprint":
        return "preprint"
    return value


def unique_candidates(candidates: list[PdfCandidate]) -> list[PdfCandidate]:
    seen: set[str] = set()
    out: list[PdfCandidate] = []
    for candidate in candidates:
        if candidate.url not in seen:
            out.append(candidate)
            seen.add(candidate.url)
    return out
