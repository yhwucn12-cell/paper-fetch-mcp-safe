"""Semantic Scholar Graph API OA PDF discovery."""

from __future__ import annotations

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError

BASE = "https://api.semanticscholar.org/graph/v1/paper"
FIELDS = "title,year,externalIds,openAccessPdf"


class SemanticScholarProvider:
    name = "semantic_scholar"
    priority = 30
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.doi)

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.http.config.s2_api_key} if self.http.config.s2_api_key else {}

    def _candidate(self, data: dict | None) -> list[PdfCandidate]:
        pdf = (data or {}).get("openAccessPdf") or {}
        if not pdf.get("url"):
            return []
        return [PdfCandidate(url=pdf["url"], provider=self.name, oa_status="oa")]

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        try:
            data = self.http.get_json(
                f"{BASE}/DOI:{record.doi}", params={"fields": FIELDS}, headers=self._headers()
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        return self._candidate(data)

    def find_pdf_batch(self, dois: list[str]) -> dict[str, list[PdfCandidate]]:
        out = {doi.lower(): [] for doi in dois}
        for offset in range(0, len(dois), 500):
            chunk = dois[offset:offset + 500]
            try:
                data = self.http.post_json(
                    f"{BASE}/batch",
                    {"ids": [f"DOI:{doi}" for doi in chunk]},
                    params={"fields": FIELDS},
                    headers=self._headers(),
                )
            except FetchError:
                raise
            except Exception as exc:
                raise ProviderError(self.name, f"batch lookup failed: {exc}") from exc
            for doi, paper in zip(chunk, data):
                out[doi.lower()] = self._candidate(paper)
        return out
