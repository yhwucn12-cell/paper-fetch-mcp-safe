"""CORE repository OA PDF fallback."""

from __future__ import annotations

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError


class CoreProvider:
    name = "core"
    priority = 40
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.doi and self.http.config.core_api_key)

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        if not self.http.config.core_api_key:
            return []
        try:
            data = self.http.get_json(
                "https://api.core.ac.uk/v3/search/works",
                params={"q": f'doi:"{record.doi}"', "limit": 3},
                headers={"Authorization": f"Bearer {self.http.config.core_api_key}"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        out = []
        for work in data.get("results") or []:
            if (work.get("doi") or "").lower() != record.doi.lower():
                continue
            if pdf := work.get("downloadUrl"):
                out.append(PdfCandidate(url=pdf, provider=self.name, oa_status="oa"))
        return out
