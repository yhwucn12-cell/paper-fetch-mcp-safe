"""Europe PMC metadata, PMID mapping, and OA-subset PDF discovery."""

from __future__ import annotations

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
PDF_BASE = "https://europepmc.org/api/getPdf"


class EuropePmcProvider:
    name = "europepmc"
    priority = 5
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.pmcid)

    def _lookup(self, query: str, limit: int = 1) -> list[dict]:
        try:
            data = self.http.get_json(f"{BASE}/search", params={
                "query": query,
                "format": "json",
                "resultType": "core",
                "pageSize": max(1, min(limit, 100)),
            })
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        return (data.get("resultList") or {}).get("result") or []

    @staticmethod
    def _fill(record: PaperRecord, item: dict) -> PaperRecord:
        record.title = record.title or item.get("title")
        record.doi = record.doi or item.get("doi")
        record.pmid = record.pmid or item.get("pmid")
        record.pmcid = record.pmcid or item.get("pmcid")
        if record.year is None:
            raw = item.get("pubYear")
            record.year = int(raw) if str(raw).isdigit() else None
        record.oa_status = record.oa_status or ("oa" if item.get("isOpenAccess") == "Y" else None)
        return record

    def enrich(self, record: PaperRecord) -> PaperRecord:
        if record.pmid:
            query = f"EXT_ID:{record.pmid} AND SRC:MED"
        elif record.pmcid:
            query = f"PMCID:{record.pmcid}"
        elif record.doi:
            query = f"DOI:{record.doi}"
        else:
            return record
        items = self._lookup(query)
        for item in items:
            if record.doi and str(item.get("doi") or "").lower() == record.doi.lower():
                return self._fill(record, item)
            if record.pmid and str(item.get("pmid") or "") == record.pmid:
                return self._fill(record, item)
            if record.pmcid and str(item.get("pmcid") or "").upper() == record.pmcid.upper():
                return self._fill(record, item)
        return record

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        if not record.pmcid:
            return []
        items = self._lookup(f"PMCID:{record.pmcid}")
        if not items:
            return []
        item = next((entry for entry in items if str(entry.get("pmcid") or "").upper() == record.pmcid.upper()), None)
        if item is None:
            return []
        self._fill(record, item)
        if item.get("isOpenAccess") != "Y":
            return []
        return [PdfCandidate(
            url=f"{PDF_BASE}?pmcid={record.pmcid}",
            provider=self.name,
            license=item.get("license"),
            version=None,
            oa_status="oa",
        )]

    def search(self, query: str, limit: int, year_min: int | None = None, year_max: int | None = None) -> list[PaperRecord]:
        terms = [query]
        if year_min or year_max:
            terms.append(f"FIRST_PDATE:[{year_min or 1800}-01-01 TO {year_max or 2100}-12-31]")
        items = self._lookup(" AND ".join(terms), limit)
        out: list[PaperRecord] = []
        for item in items:
            record = self._fill(PaperRecord(), item)
            if item.get("isOpenAccess") == "Y" and record.pmcid:
                record.pdf_candidates.append(PdfCandidate(
                    url=f"{PDF_BASE}?pmcid={record.pmcid}",
                    provider=self.name,
                    license=item.get("license"),
                    version=None,
                ))
            out.append(record)
        return out
