"""Published Zenodo article and preprint files, with DOI identity checks."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError

BASE = "https://zenodo.org/api/records"
ZENODO_DOI = re.compile(r"^10\.5281/zenodo\.(\d+)$", re.I)
_SUPPLEMENT = re.compile(r"(?:supp|support|appendix|poster|slides|presentation)", re.I)
_RELATED = {"ispreprintof", "isversionof", "isidenticalto"}


def _doi(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", str(value).strip(), flags=re.I).lower()


class ZenodoProvider:
    name = "zenodo"
    priority = 15
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.doi or record.extra.get("zenodo_id"))

    @staticmethod
    def _is_publication(item: dict) -> bool:
        metadata = item.get("metadata") or {}
        kind = metadata.get("upload_type") or metadata.get("resource_type") or ""
        if isinstance(kind, dict):
            kind = kind.get("id") or kind.get("type") or ""
        return "publication" in str(kind).lower() or str(kind).lower() in {"article", "preprint"}

    @staticmethod
    def _matches(item: dict, doi: str | None, exact_id: bool) -> bool:
        if exact_id:
            return True
        expected = _doi(doi)
        metadata = item.get("metadata") or {}
        pids = item.get("pids") or {}
        own = [_doi(item.get("doi")), _doi(metadata.get("doi")),
               _doi((pids.get("doi") or {}).get("identifier") if isinstance(pids.get("doi"), dict) else None)]
        if expected in own:
            return True
        for related in metadata.get("related_identifiers") or []:
            if (_doi(related.get("identifier")) == expected
                    and str(related.get("relation") or "").lower() in _RELATED):
                return True
        return False

    @staticmethod
    def _file_entries(item: dict) -> list[dict]:
        files = item.get("files") or []
        if isinstance(files, dict):
            files = files.get("entries") or {}
        return list(files.values()) if isinstance(files, dict) else files if isinstance(files, list) else []

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        exact_id = record.extra.get("zenodo_id")
        if not exact_id and record.doi:
            match = ZENODO_DOI.fullmatch(record.doi)
            exact_id = match.group(1) if match else None
        try:
            if exact_id:
                items = [self.http.get_json(f"{BASE}/{exact_id}")]
            elif record.doi:
                escaped = record.doi.replace("\\", "\\\\").replace('"', '\\"')
                data = self.http.get_json(BASE, params={"q": f'"{escaped}"', "size": 10, "type": "Publication"})
                items = (data.get("hits") or {}).get("hits") or []
            else:
                return []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        candidates = []
        for item in items:
            if not isinstance(item, dict) or not self._is_publication(item) or not self._matches(item, record.doi, bool(exact_id)):
                continue
            metadata = item.get("metadata") or {}
            access = item.get("access") or {}
            status = str(access.get("status") or metadata.get("access_right") or "open").lower()
            if status not in {"open", "open_access", "public"}:
                continue
            record.title = record.title or metadata.get("title")
            record.doi = record.doi or item.get("doi") or metadata.get("doi")
            record.extra.setdefault("zenodo_id", str(item.get("id") or exact_id or ""))
            license_data = metadata.get("license") or {}
            license_name = (license_data.get("id") or license_data.get("title")) if isinstance(license_data, dict) else str(license_data) or None
            resource_type = metadata.get("resource_type") or {}
            subtype = str(metadata.get("publication_type") or
                          (resource_type.get("subtype") or resource_type.get("id")
                           if isinstance(resource_type, dict) else resource_type) or "").lower()
            for file in self._file_entries(item):
                name = str(file.get("key") or file.get("filename") or file.get("name") or "")
                if not name.lower().endswith(".pdf") or _SUPPLEMENT.search(name):
                    continue
                links = file.get("links") or {}
                url = links.get("content") or links.get("download") or links.get("self")
                host = urlparse(url).hostname or "" if isinstance(url, str) else ""
                if not isinstance(url, str) or urlparse(url).scheme != "https" or not (host == "zenodo.org" or host.endswith(".zenodo.org")):
                    continue
                candidates.append(PdfCandidate(
                    url=url, provider=self.name, license=license_name,
                    version="preprint" if "preprint" in subtype else None, oa_status="oa",
                ))
        return candidates
