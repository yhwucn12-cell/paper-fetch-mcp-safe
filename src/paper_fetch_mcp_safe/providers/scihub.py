"""Gray fallback (Sci-Hub mirrors), never loaded by the default registry.

Kept in the tree as an auditable reference implementation of the *refused* path: the
registry only instantiates it when ``allow_scihub = true``, and every candidate it
returns is labelled ``compliance_tier = "gray"`` so the licence policy can identify it.
Do not enable it without checking local law and your institution's policy.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin, urlparse

from ..http import GRAY_USER_AGENT, HttpClient
from ..models import FetchError, PaperRecord, PdfCandidate
from .base import ProviderError

EMBED_RE = re.compile(r'<(?:embed|iframe)[^>]+src=["\']([^"\']+)["\']', re.I)


class SciHubProvider:
    name = "scihub"
    priority = 100
    compliance_tier = "gray"

    def __init__(self, http: HttpClient):
        self.http = http
        for mirror in http.config.scihub_mirrors:
            host = urlparse(mirror).hostname
            if host:
                http.limiter.set_rate(host, 0.2)

    def supports(self, record: PaperRecord) -> bool:
        return bool(record.doi)

    def find_pdf(self, record: PaperRecord) -> list[PdfCandidate]:
        """Collect embeds from every reachable mirror so one dead CDN does not end the path."""
        last_error: FetchError | None = None
        fetched_page = False
        candidates: list[PdfCandidate] = []
        seen: set[str] = set()
        for mirror in self.http.config.scihub_mirrors:
            try:
                page = self.http.get_text(f"{mirror}/{record.doi}",
                                          headers={"User-Agent": GRAY_USER_AGENT})
                fetched_page = True
            except FetchError as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = ProviderError(self.name, str(exc))
                continue
            for match in EMBED_RE.finditer(page):
                raw = unescape(match.group(1))
                url = ("https:" + raw if raw.startswith("//") else urljoin(mirror + "/", raw)).split("#", 1)[0]
                if url.startswith("https://") and url not in seen:
                    seen.add(url)
                    candidates.append(PdfCandidate(url=url, provider=self.name, compliance_tier="gray"))
        if not candidates and last_error and not fetched_page:
            raise last_error
        return candidates
