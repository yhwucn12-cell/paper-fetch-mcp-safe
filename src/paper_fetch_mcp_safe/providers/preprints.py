"""Precise conference and preprint identifiers using official source records."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import quote, urljoin

import httpx

from ..http import HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError


class PreprintProvider:
    name = "preprints"
    priority = 50
    compliance_tier = "oa"

    def __init__(self, http: HttpClient):
        self.http = http

    @staticmethod
    def _page_pdf(html: str, base: str) -> str | None:
        for pattern in (
            r'<meta\b[^>]*name=["\']citation_pdf_url["\'][^>]*content=["\']([^"\']+)["\']',
            r'<a\b[^>]*href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        ):
            if match := re.search(pattern, html, re.I):
                url = urljoin(base, unescape(match.group(1)))
                if url.startswith("https://"):
                    return url
        return None

    def supports(self, record: PaperRecord) -> bool:
        return bool((record.doi or "").startswith("10.1101/") or record.extra)

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        if record.extra.get("openreview_id"):
            return self._openreview(record)
        if record.extra.get("acl_id"):
            return self._acl(record)
        if record.extra.get("pmlr_id"):
            return self._pmlr(record)
        if (record.doi or "").startswith("10.1101/"):
            return self._rxiv(record)
        return []

    def _openreview(self, record: PaperRecord) -> list[PdfCandidate]:
        oid = record.extra["openreview_id"]
        try:
            data = self.http.get_json("https://api2.openreview.net/notes", params={"id": oid})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"OpenReview HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        notes = data.get("notes") or []
        if not notes:
            return []
        note = notes[0]
        title = (note.get("content") or {}).get("title")
        if isinstance(title, dict):
            title = title.get("value")
        record.title = record.title or title
        if not (note.get("pdf") or (note.get("content") or {}).get("pdf")):
            return []
        return [PdfCandidate(url=f"https://openreview.net/pdf?id={quote(oid)}", provider=self.name)]

    def _acl(self, record: PaperRecord) -> list[PdfCandidate]:
        aid = record.extra["acl_id"]
        try:
            html = self.http.get_text(f"https://aclanthology.org/{quote(aid)}/")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"ACL HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        if title := re.search(r'<meta\s+name="citation_title"\s+content="([^"]+)"', html, re.I):
            record.title = record.title or title.group(1)
        pdf = self._page_pdf(html, f"https://aclanthology.org/{quote(aid)}/") or f"https://aclanthology.org/{quote(aid)}.pdf"
        return [PdfCandidate(url=pdf, provider=self.name, version="vor")]

    def _pmlr(self, record: PaperRecord) -> list[PdfCandidate]:
        pid = record.extra["pmlr_id"].strip("/").removesuffix(".html")
        parts = pid.split("/")
        if len(parts) != 2 or not re.fullmatch(r"v\d+", parts[0]) or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]):
            return []
        volume, slug = parts
        try:
            html = self.http.get_text(f"https://proceedings.mlr.press/{volume}/{slug}.html")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise ProviderError(self.name, f"PMLR HTTP {exc.response.status_code}") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc
        pdf = self._page_pdf(html, f"https://proceedings.mlr.press/{volume}/{slug}.html")
        pdf = pdf or f"https://proceedings.mlr.press/{volume}/{slug}/{slug}.pdf"
        return [PdfCandidate(url=pdf, provider=self.name, version="vor")]

    def _rxiv(self, record: PaperRecord) -> list[PdfCandidate]:
        for server in ("biorxiv", "medrxiv"):
            try:
                data = self.http.get_json(f"https://api.biorxiv.org/details/{server}/{record.doi}")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    continue
                raise ProviderError(self.name, f"{server} HTTP {exc.response.status_code}") from exc
            except FetchError:
                raise
            except Exception as exc:
                raise ProviderError(self.name, str(exc)) from exc
            records = data.get("collection") or []
            if not records:
                continue
            item = max(records, key=lambda x: int(x.get("version") or 0))
            record.title = record.title or item.get("title")
            version = str(item.get("version") or "1")
            return [PdfCandidate(
                url=f"https://www.{server}.org/content/{record.doi}v{version}.full.pdf",
                provider=self.name,
                license=item.get("license"),
                version="preprint",
            )]
        return []
