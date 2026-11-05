"""Unpaywall OA locations by DOI."""

from __future__ import annotations

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError, normalized_version, unique_candidates


class UnpaywallProvider:
    name = "unpaywall"
    priority = 20
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.doi)

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        try:
            data = self.http.get_json(f"https://api.unpaywall.org/v2/{record.doi}", params={"email": self.http.config.mailto})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        if not data.get("is_oa"):
            return []
        out: list[PdfCandidate] = []
        for location in [data.get("best_oa_location"), *(data.get("oa_locations") or [])]:
            if not location or not location.get("url_for_pdf"):
                continue
            out.append(PdfCandidate(
                url=location["url_for_pdf"],
                provider=self.name,
                license=location.get("license"),
                version=normalized_version(location.get("version")),
                compliance_tier="oa",
                oa_status=data.get("oa_status"),
            ))
        return unique_candidates(out)
