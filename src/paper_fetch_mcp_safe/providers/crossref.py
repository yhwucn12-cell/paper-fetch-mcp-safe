"""Crossref DOI metadata only; publisher TDM links are intentionally ignored."""

from __future__ import annotations

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord
from .base import ProviderError


class CrossrefProvider:
    name = "crossref"

    def __init__(self, http: HttpClient):
        self.http = http

    def enrich(self, record: PaperRecord) -> PaperRecord:
        if not record.doi:
            return record
        try:
            data = self.http.get_json(
                f"https://api.crossref.org/works/{record.doi}",
                params={"mailto": self.http.config.mailto},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                record.extra["crossref_status"] = "not_found"
                return record
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        work = data.get("message") or {}
        record.extra["crossref_status"] = "found"
        record.title = record.title or ((work.get("title") or [None])[0])
        if record.year is None:
            for key in ("published", "published-print", "published-online", "issued"):
                dates = (work.get(key) or {}).get("date-parts") or []
                if dates and dates[0]:
                    record.year = dates[0][0]
                    break
        return record
