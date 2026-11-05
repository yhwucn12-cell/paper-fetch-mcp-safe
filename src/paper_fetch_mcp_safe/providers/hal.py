"""HAL open repository records with an exact DOI or HAL identifier."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError

BASE = "https://api.hal.science/search/"


def _first(value):
    return (value[0] if value else None) if isinstance(value, list) else value


class HalProvider:
    name = "hal"
    priority = 12
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.doi or record.extra.get("hal_id"))

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        hal_id = record.extra.get("hal_id")
        version = re.search(r"v(\d+)$", hal_id) if hal_id else None
        if hal_id:
            base_id = re.sub(r"v\d+$", "", hal_id)
            query = f'halId_s:"{base_id}"'
        elif record.doi:
            escaped = record.doi.replace("\\", "\\\\").replace('"', '\\"')
            query = f'doiId_s:"{escaped}"'
        else:
            return []
        try:
            filters = ["submitType_s:file"]
            if version:
                filters.append(f"version_i:{int(version.group(1))}")
            data = self.http.get_json(BASE, params={
                "q": query, "fq": filters, "wt": "json", "rows": 5,
                "fl": "halId_s,doiId_s,title_s,fileMain_s,fileType_s,fileLicenses_s,version_i",
            })
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        result = []
        for item in (data.get("response") or {}).get("docs") or []:
            found_dois = item.get("doiId_s") or []
            if isinstance(found_dois, str):
                found_dois = [found_dois]
            if record.doi and not any(str(doi).lower() == record.doi.lower() for doi in found_dois):
                continue
            found_id = str(item.get("halId_s") or "").lower()
            if hal_id and re.sub(r"v\d+$", "", found_id) != re.sub(r"v\d+$", "", hal_id.lower()):
                continue
            if version and int(item.get("version_i") or 0) != int(version.group(1)):
                continue
            urls = item.get("fileMain_s") or []
            if isinstance(urls, str):
                urls = [urls]
            for url in urls[:3]:
                if not isinstance(url, str) or urlparse(url).scheme != "https":
                    continue
                record.title = record.title or _first(item.get("title_s"))
                record.doi = record.doi or _first(found_dois)
                record.extra.setdefault("hal_id", found_id)
                result.append(PdfCandidate(
                    url=url, provider=self.name,
                    license=_first(item.get("fileLicenses_s")), oa_status="oa",
                ))
        return result
