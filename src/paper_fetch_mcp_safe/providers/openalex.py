"""OpenAlex OA location discovery; PDF content remains in the shared downloader."""

from __future__ import annotations

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError, normalized_version, unique_candidates

BASE = "https://api.openalex.org/works"


class OpenAlexProvider:
    name = "openalex"
    priority = 10
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.doi)

    def _params(self, **kwargs) -> dict:
        params = {"mailto": self.http.config.mailto, **kwargs}
        if self.http.config.openalex_api_key:
            params["api_key"] = self.http.config.openalex_api_key
        return params

    def candidates_from_work(self, work: dict) -> list[PdfCandidate]:
        status = (work.get("open_access") or {}).get("oa_status")
        locations = [work.get("best_oa_location"), *(work.get("locations") or [])]
        out: list[PdfCandidate] = []
        for location in locations:
            if not location:
                continue
            pdf = location.get("pdf_url")
            if not pdf or not pdf.startswith(("http://", "https://")):
                continue
            is_oa = location.get("is_oa")
            if is_oa is False or (is_oa is None and not (work.get("open_access") or {}).get("is_oa")):
                continue
            out.append(PdfCandidate(
                url=pdf,
                provider=self.name,
                license=location.get("license"),
                version=normalized_version(location.get("version")),
                compliance_tier="oa",
                oa_status=status,
            ))
        return unique_candidates(out)

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        try:
            data = self.http.get_json(f"{BASE}/https://doi.org/{record.doi}", params=self._params())
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        return self.candidates_from_work(data)

    def find_pdf_batch(self, dois: list[str]) -> dict[str, list[PdfCandidate]]:
        out: dict[str, list[PdfCandidate]] = {doi.lower(): [] for doi in dois}
        for offset in range(0, len(dois), 50):
            chunk = dois[offset:offset + 50]
            try:
                data = self.http.get_json(BASE, params=self._params(
                    filter="doi:" + "|".join(f"https://doi.org/{doi}" for doi in chunk),
                    select="doi,best_oa_location,locations,open_access",
                    per_page=50,
                ))
            except FetchError:
                raise
            except Exception as exc:
                raise ProviderError(self.name, f"batch lookup failed: {exc}") from exc
            for work in data.get("results", []):
                doi = (work.get("doi") or "").removeprefix("https://doi.org/").lower()
                if doi in out:
                    out[doi] = self.candidates_from_work(work)
        return out

    def search(self, query: str, limit: int = 10, year_min: int | None = None, year_max: int | None = None) -> list[PaperRecord]:
        filters = []
        if year_min:
            filters.append(f"from_publication_date:{year_min}-01-01")
        if year_max:
            filters.append(f"to_publication_date:{year_max}-12-31")
        params = self._params(search=query, per_page=min(limit, 100))
        if filters:
            params["filter"] = ",".join(filters)
        try:
            data = self.http.get_json(BASE, params=params)
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, f"search failed: {exc}") from exc
        out = []
        for work in data.get("results", []):
            out.append(PaperRecord(
                title=work.get("title"),
                doi=(work.get("doi") or "").removeprefix("https://doi.org/") or None,
                year=work.get("publication_year"),
                oa_status=(work.get("open_access") or {}).get("oa_status"),
                pdf_candidates=self.candidates_from_work(work),
            ))
        return out
