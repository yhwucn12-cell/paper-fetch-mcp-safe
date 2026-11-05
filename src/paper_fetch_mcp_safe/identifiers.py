"""Parse user-facing identifiers into a normalized paper record."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from .models import ErrorCode, FetchError, PaperRecord

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.I)
ARXIV_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.I)
PMCID_RE = re.compile(r"^PMC\d+$", re.I)
PMID_RE = re.compile(r"^\d{1,9}$")
ACL_RE = re.compile(r"^\d{4}\.[a-z-]+\.\d+$", re.I)
PMLR_RE = re.compile(r"^v\d+/[A-Za-z0-9_-]+$")
OPENREVIEW_RE = re.compile(r"^[A-Za-z0-9_-]+$")
OSF_RE = re.compile(r"^[a-z0-9]{5}(?:_v\d+)?$", re.I)
ZENODO_RE = re.compile(r"^\d+$")
HAL_RE = re.compile(r"^hal-\d+(?:v\d+)?$", re.I)


def _is_host(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def parse_identifier(value: str) -> PaperRecord:
    raw = value.strip()
    if not raw:
        raise FetchError(ErrorCode.BAD_IDENTIFIER, "identifier is empty")
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        host = (parsed.hostname or "").lower()
        if host in {"doi.org", "dx.doi.org"}:
            raw = unquote(parsed.path.lstrip("/"))
        elif _is_host(host, "arxiv.org"):
            raw = parsed.path.removeprefix("/abs/").removeprefix("/pdf/").removesuffix(".pdf").strip("/")
        elif _is_host(host, "openreview.net"):
            raw = "openreview:" + (parse_qs(parsed.query).get("id") or [""])[0]
        elif _is_host(host, "aclanthology.org"):
            raw = "acl:" + parsed.path.strip("/").removesuffix(".pdf")
        elif _is_host(host, "proceedings.mlr.press"):
            raw = "pmlr:" + parsed.path.strip("/").removesuffix(".html").removesuffix(".pdf")
        elif _is_host(host, "ncbi.nlm.nih.gov") and "articles" in parsed.path:
            raw = parsed.path.rstrip("/").split("/")[-1]
        elif _is_host(host, "zenodo.org") and parsed.path.startswith(("/record/", "/records/")):
            raw = "zenodo:" + parsed.path.strip("/").split("/")[-1]
        elif _is_host(host, "hal.science") and HAL_RE.fullmatch(parsed.path.strip("/").split("/")[0]):
            raw = "hal:" + parsed.path.strip("/").split("/")[0]
        elif host == "osf.io":
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 1 or (len(parts) == 3 and parts[0] == "preprints"):
                raw = "osf:" + parts[-1]
            else:
                raise FetchError(ErrorCode.BAD_IDENTIFIER, f"unsupported OSF URL: {value}")
        else:
            raise FetchError(ErrorCode.BAD_IDENTIFIER, f"unsupported identifier URL: {value}")
    low = raw.lower()
    for prefix in ("doi:", "pmid:", "pmcid:", "arxiv:"):
        if low.startswith(prefix):
            raw = raw[len(prefix):]
            low = raw.lower()
            break
    if DOI_RE.fullmatch(raw):
        return PaperRecord(doi=raw)
    if PMCID_RE.fullmatch(raw):
        return PaperRecord(pmcid=raw.upper())
    if low.startswith("openreview:") and OPENREVIEW_RE.fullmatch(raw.split(":", 1)[1]):
        return PaperRecord(extra={"openreview_id": raw.split(":", 1)[1]})
    if low.startswith("acl:") and ACL_RE.fullmatch(raw.split(":", 1)[1]):
        return PaperRecord(extra={"acl_id": raw.split(":", 1)[1]})
    if low.startswith("pmlr:") and raw.split(":", 1)[1]:
        path = raw.split(":", 1)[1]
        parts = path.split("/")
        if len(parts) == 3 and parts[-1] == parts[-2]:
            path = "/".join(parts[:2])
        if PMLR_RE.fullmatch(path):
            return PaperRecord(extra={"pmlr_id": path})
    if low.startswith("osf:") and OSF_RE.fullmatch(raw.split(":", 1)[1]):
        return PaperRecord(extra={"osf_id": raw.split(":", 1)[1].lower()})
    if low.startswith("zenodo:") and ZENODO_RE.fullmatch(raw.split(":", 1)[1]):
        return PaperRecord(extra={"zenodo_id": raw.split(":", 1)[1]})
    if low.startswith("hal:") and HAL_RE.fullmatch(raw.split(":", 1)[1]):
        return PaperRecord(extra={"hal_id": raw.split(":", 1)[1].lower()})
    if ARXIV_RE.fullmatch(raw):
        return PaperRecord(arxiv_id=raw)
    if PMID_RE.fullmatch(raw):
        return PaperRecord(pmid=raw)
    raise FetchError(ErrorCode.BAD_IDENTIFIER, f"unrecognized identifier: {value}")
