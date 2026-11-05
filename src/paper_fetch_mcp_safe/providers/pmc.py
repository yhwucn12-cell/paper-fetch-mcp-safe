"""PMC identifier conversion and the current OA Article Datasets cloud service."""

from __future__ import annotations

import re
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError

ID_CONVERTER = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
_VERSION = re.compile(r"^PMC\d+\.\d+/$")


class PmcIdConverter:
    """Find a PMCID for a DOI not mapped by Europe PMC."""

    name = "pmc_id_converter"

    def __init__(self, http: HttpClient):
        self.http = http

    def lookup_many(self, dois: list[str]) -> dict[str, dict | None]:
        """Convert DOI batches using the documented 200-ID request limit."""
        unique = list(dict.fromkeys(doi.lower() for doi in dois))
        out: dict[str, dict | None] = {doi: None for doi in unique}
        for start in range(0, len(unique), 200):
            chunk = unique[start:start + 200]
            try:
                data = self.http.get_json(ID_CONVERTER, params={
                    "ids": ",".join(chunk), "idtype": "doi", "format": "json",
                    "tool": "paper-fetch-mcp-safe", "email": self.http.config.mailto,
                })
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    continue
                raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
            except FetchError:
                raise
            except Exception as exc:
                raise ProviderError(self.name, str(exc)) from exc
            for item in data.get("records") or []:
                requested = str(item.get("requested-id") or "").lower()
                pmcid = item.get("pmcid")
                if requested in out and isinstance(pmcid, str) and re.fullmatch(r"PMC\d+", pmcid, re.I):
                    out[requested] = {"pmcid": pmcid.upper(), "pmid": str(item.get("pmid") or "") or None}
        return out

    def enrich(self, record: PaperRecord) -> PaperRecord:
        identifier = record.doi or record.pmid
        if not identifier or record.pmcid:
            return record
        try:
            data = self.http.get_json(ID_CONVERTER, params={
                "ids": identifier, "format": "json", "tool": "paper-fetch-mcp-safe",
                "email": self.http.config.mailto,
            })
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return record
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        for item in data.get("records") or []:
            requested = item.get("requested-id") or identifier
            if str(requested).lower() != identifier.lower():
                continue
            pmcid = item.get("pmcid")
            if isinstance(pmcid, str) and re.fullmatch(r"PMC\d+", pmcid, re.I):
                record.pmcid = pmcid.upper()
                record.pmid = record.pmid or str(item.get("pmid") or "") or None
                break
        return record


class PmcCloudProvider:
    """Find PDFs in the public PMC Article Datasets, by exact PMCID."""

    name = "pmc_cloud"
    priority = 8
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.pmcid)

    @staticmethod
    def _https_url(value: str | None) -> str | None:
        if not isinstance(value, str):
            return None
        if value.startswith("s3://pmc-oa-opendata/"):
            return BUCKET + "/" + value.removeprefix("s3://pmc-oa-opendata/")
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.hostname == "pmc-oa-opendata.s3.amazonaws.com":
            return value
        return None

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        if not record.pmcid:
            return []
        try:
            listing = self.http.get_text(BUCKET + "/", params={
                "list-type": "2", "prefix": record.pmcid + ".", "delimiter": "/",
            })
            root = ElementTree.fromstring(listing)
            prefixes = []
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] == "Prefix" and node.text and _VERSION.fullmatch(node.text):
                    if node.text.startswith(record.pmcid + "."):
                        prefixes.append(node.text)
            candidates: list[PdfCandidate] = []
            for prefix in sorted(set(prefixes)):
                data = self.http.get_json(f"{BUCKET}/metadata/{prefix.rstrip('/')}.json")
                if str(data.get("pmcid") or "").upper() != record.pmcid:
                    continue
                if str(data.get("is_pmc_openaccess") or "").lower() not in {"yes", "true", "1"}:
                    continue
                if str(data.get("is_retracted") or "").lower() in {"yes", "true", "1"}:
                    continue
                license_code = str(data.get("license_code") or "")
                if license_code.upper() == "TDM":
                    continue
                if pdf := self._https_url(data.get("pdf_url")):
                    candidates.append(PdfCandidate(
                        url=pdf, provider=self.name, license=license_code or None,
                        version="am" if str(data.get("is_manuscript") or "").lower() in {"yes", "true", "1"} else None,
                        oa_status="oa",
                    ))
            if not candidates:
                record.extra["pmc_dataset_pdf"] = "not_available"
            return sorted(candidates, key=lambda candidate: candidate.version == "am")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                record.extra["pmc_dataset_pdf"] = "not_available"
                return []
            raise ProviderError(self.name, f"HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
