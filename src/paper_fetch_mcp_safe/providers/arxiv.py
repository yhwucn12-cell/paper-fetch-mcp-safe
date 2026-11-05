"""arXiv Atom API through the shared HTTP and arXiv host gate."""

from __future__ import annotations

import re
from xml.etree import ElementTree

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError

BASE = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


def _search_query(query: str) -> str:
    """Join bare topic words with AND; pass through anything that already speaks arXiv syntax."""
    # arXiv reads a space-separated query as OR, which turns a topic into noise.
    text = " ".join(query.split())
    if not text or re.search(r"\b(AND|OR|ANDNOT)\b", text) or re.match(r"^\s*\w+:", text):
        return text
    terms = [term for term in text.split(" ") if term]
    return " AND ".join(f"all:{term}" for term in terms) if terms else text


class ArxivProvider:
    name = "arxiv"
    priority = 6
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.arxiv_id)

    def _lookup(self, params: dict) -> list[PaperRecord]:
        try:
            xml = self.http.get_text(BASE, params=params)
            root = ElementTree.fromstring(xml)
        except httpx.HTTPStatusError as exc:
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        records: list[PaperRecord] = []
        for entry in root.findall(f"{ATOM}entry"):
            raw_id = (entry.findtext(f"{ATOM}id") or "").rstrip("/").split("/")[-1]
            arxiv_id = re.sub(r"v\d+$", "", raw_id)
            if not arxiv_id:
                continue
            published = entry.findtext(f"{ATOM}published") or ""
            pdf = next((link.get("href") for link in entry.findall(f"{ATOM}link") if link.get("type") == "application/pdf"), None)
            record = PaperRecord(
                title=" ".join((entry.findtext(f"{ATOM}title") or "").split()),
                arxiv_id=arxiv_id,
                doi=entry.findtext(f"{ARXIV}doi"),
                year=int(published[:4]) if published[:4].isdigit() else None,
                oa_status="preprint",
            )
            if pdf:
                record.pdf_candidates.append(PdfCandidate(url=pdf, provider=self.name, version="preprint", oa_status="preprint"))
            records.append(record)
        return records

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        if not record.arxiv_id:
            return []
        found = self._lookup({"id_list": record.arxiv_id, "max_results": 1})
        if not found:
            return []
        match = found[0]
        record.title = record.title or match.title
        record.doi = record.doi or match.doi
        return match.pdf_candidates

    def search(self, query: str, limit: int, year_min: int | None = None, year_max: int | None = None) -> list[PaperRecord]:
        records = self._lookup({"search_query": _search_query(query), "start": 0, "max_results": min(limit, 100)})
        return [r for r in records if (year_min is None or (r.year or 0) >= year_min)
                and (year_max is None or (r.year or 9999) <= year_max)]

