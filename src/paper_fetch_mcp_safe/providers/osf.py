"""Public OSF Preprints by exact GUID, version, or osf.io DOI."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError

BASE = "https://api.osf.io/v2/preprints"
OSF_DOI = re.compile(r"^10\.\d{4,9}/osf\.io/([a-z0-9]{5})$", re.I)


class OsfPreprintsProvider:
    name = "osf_preprints"
    priority = 7
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.extra.get("osf_id") or (record.doi and OSF_DOI.fullmatch(record.doi)))

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        osf_id = record.extra.get("osf_id")
        if not osf_id and record.doi:
            match = OSF_DOI.fullmatch(record.doi)
            osf_id = match.group(1) if match else None
        if not osf_id:
            return []
        ids = [osf_id]
        if "_v" not in osf_id:
            ids.append(osf_id + "_v1")
        data = None
        try:
            for identifier in ids:
                try:
                    data = self.http.get_json(f"{BASE}/{identifier}/")
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
        except httpx.HTTPStatusError as exc:
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        item = (data or {}).get("data") or {}
        if not item:
            return []
        attrs = item.get("attributes") or {}
        if attrs.get("public") is not True or attrs.get("is_published") is not True:
            return []
        if attrs.get("date_withdrawn") or attrs.get("reviews_state") in {"rejected", "withdrawn"}:
            return []
        primary = ((item.get("relationships") or {}).get("primary_file") or {}).get("data")
        if not isinstance(primary, dict) or not primary.get("id"):
            return []
        try:
            file_data = self.http.get_json(f"https://api.osf.io/v2/files/{primary['id']}/")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"file HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        file_item = file_data.get("data") or {}
        if str(file_item.get("id") or "") != str(primary["id"]):
            return []
        filename = str((file_item.get("attributes") or {}).get("name") or "")
        url = (file_item.get("links") or {}).get("download")
        if not filename.lower().endswith(".pdf") or not isinstance(url, str):
            return []
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "osf.io":
            return []
        found_doi = attrs.get("doi") or (item.get("links") or {}).get("preprint_doi")
        normalized = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(found_doi), flags=re.I) if found_doi else None
        if record.doi and (not normalized or normalized.lower() != record.doi.lower()):
            return []
        exact_id = str(item.get("id") or osf_id)
        record.title = record.title or attrs.get("title")
        record.doi = record.doi or (normalized if found_doi else None)
        record.extra["osf_id"] = exact_id
        license_data = ((item.get("relationships") or {}).get("license") or {}).get("data") or {}
        return [PdfCandidate(
            url=url, provider=self.name,
            license=license_data.get("id"), version="preprint", oa_status="oa",
        )]
