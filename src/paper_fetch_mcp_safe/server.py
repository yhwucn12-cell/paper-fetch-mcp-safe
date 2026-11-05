"""FastMCP tools and the paper retrieval workflow."""

from __future__ import annotations

import hashlib
import itertools
import argparse
import json
import logging
import os
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP

from . import compliance
from .config import Config, ConfigError
from .http import HttpClient
from .identifiers import parse_identifier
from .models import ErrorCode, FetchError, FetchResult, PaperRecord, PdfCandidate, redact_secrets
from .providers.arxiv import ArxivProvider
from .providers.base import unique_candidates
from .providers.crossref import CrossrefProvider
from .providers.europepmc import EuropePmcProvider
from .providers.openalex import OpenAlexProvider
from .providers.pmc import PmcIdConverter
from .router import build_registry, resolve_candidates

LOGGER = logging.getLogger("paper_fetch_mcp_safe")
mcp = FastMCP("paper-fetch-mcp-safe")
_state: "ServerState | None" = None


def _error_result(identifier: str, error: FetchError) -> dict[str, Any]:
    return FetchResult(ok=False, identifier=identifier, error=error.to_dict()).to_dict()


def _best_error(errors: list[dict[str, Any]], *, had_candidate: bool) -> dict[str, Any]:
    for code in (ErrorCode.RATE_LIMITED, ErrorCode.DOWNLOAD_CORRUPT, ErrorCode.PROVIDER_UNAVAILABLE,
                 ErrorCode.LICENSE_RESTRICTED):
        for error in errors:
            if error.get("code") == code.value:
                return {k: v for k, v in error.items() if k in {"code", "message", "retry_after"}}
    if had_candidate:
        return FetchError(ErrorCode.PROVIDER_UNAVAILABLE, "all candidate PDF links failed").to_dict()
    return FetchError(ErrorCode.PAYWALL_NO_OA, "no registered source returned an OA PDF").to_dict()


def _destination(directory: str, record: PaperRecord) -> Path:
    root = Path(directory).expanduser().resolve()
    identifier = record.best_identifier()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", identifier).strip("-.")[:90] or "paper"
    suffix = hashlib.sha256(identifier.lower().encode("utf-8")).hexdigest()[:10]
    return root / f"{slug}-{suffix}.pdf"


VERBOSITY_LEVELS = ("minimal", "full")
GRAY_WARNING = compliance.GRAY_WARNING


def _canonical_id(paper: dict[str, Any]) -> str | None:
    """Same precedence as PaperRecord.best_identifier, so minimal rows stay re-usable."""
    for key, prefix in (("pmcid", "pmcid"), ("arxiv_id", "arxiv"), ("doi", "doi"), ("pmid", "pmid")):
        if paper.get(key):
            return f"{prefix}:{paper[key]}"
    return None


def _paper_summary(paper: dict[str, Any]) -> dict[str, Any]:
    return {"title": paper.get("title"), "year": paper.get("year"),
            "doi": paper.get("doi"), "id": _canonical_id(paper)}


def _search_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {"papers": [_paper_summary(paper) for paper in result.get("papers") or []],
            "errors": result.get("errors") or []}


def _resolve_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "identifier": result.get("identifier"),
        "title": (result.get("paper") or {}).get("title"),
        "candidate_count": len(result.get("candidates") or []),
        "candidates": [{"provider": candidate.get("provider"),
                        "compliance_tier": candidate.get("compliance_tier"),
                        "license_class": compliance.license_class(
                            compliance.normalize_license(candidate.get("license")))}
                       for candidate in result.get("candidates") or []],
    }
    for key in ("error", "errors"):
        if result.get(key):
            summary[key] = result[key]
    return summary


def _resolve_directory(destination: str | None) -> str | None:
    """Resolve a caller-supplied destination, or None when it is missing or unusable."""
    if not destination or "\x00" in destination:
        # A NUL byte is rejected up front: on some platforms Path.resolve() accepts it
        # and the failure only surfaces later as an unhandled OSError mid-download.
        return None
    try:
        return str(Path(destination).expanduser().resolve())
    except (OSError, ValueError):
        return None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish JSON atomically so a concurrent reader never sees a partial file."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".paper-fetch-", suffix=".json.part",
                                     mode="w", encoding="utf-8", delete=False) as out:
        temporary = Path(out.name)
        json.dump(payload, out, ensure_ascii=False, indent=2)
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


_BATCH_SEQUENCE = itertools.count(1)


def _batch_results_path(directory: Path) -> Path:
    """A per-call name that cannot collide: pid plus a process-wide counter.

    Timestamps alone are not enough — the platform clock is coarser than the microsecond
    field suggests, so two batches in the same tick produce the identical string.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    while True:
        candidate = directory / f"batch-results-{stamp}-{os.getpid()}-{next(_BATCH_SEQUENCE)}.json"
        if not candidate.exists():
            return candidate


def _batch_summary(result: dict[str, Any], directory: str) -> dict[str, Any]:
    """Counts, failures and any gray-source disclosure; per-item records go to a JSON file."""
    items = result.get("items") or []
    summary: dict[str, Any] = {
        "succeeded": result.get("succeeded"), "failed": result.get("failed"),
        "skipped": result.get("skipped"),
        "failures": [{"identifier": item.get("identifier"), "code": (item.get("error") or {}).get("code")}
                     for item in items if not item.get("ok") and not item.get("skipped")],
    }
    if restricted := sum(1 for item in items
                         if (item.get("error") or {}).get("code") == ErrorCode.LICENSE_RESTRICTED.value):
        summary["license_restricted"] = restricted
    if gray := sum(1 for item in items if item.get("compliance_tier") == "gray"):
        # Trimming must not hide provenance: the tier and its notice travel with the summary.
        summary["gray"] = gray
        summary["warning"] = GRAY_WARNING
    if result.get("error"):
        summary["error"] = result["error"]
    path = _batch_results_path(Path(directory))
    try:
        _atomic_write_json(path, {"directory": directory, "items": items})
    except OSError as exc:
        summary["items"] = items
        summary["note"] = f"could not write {path.name} ({exc}); full items returned inline"
    else:
        summary["results_path"] = str(path)
    return summary


@dataclass(frozen=True, slots=True)
class PaperRequest:
    """A skill template as data, so validation and dispatch can be reasoned about apart."""

    action: str
    identifiers: list[str] | None = None
    query: str | None = None
    directory: str | None = None
    source: str = "auto"
    limit: int = 10
    year_min: int | None = None
    year_max: int | None = None
    concurrency: int = 4
    verbosity: str = "minimal"
    review_search_results: bool = False

    @property
    def title_download(self) -> bool:
        return self.action == "download" and bool(self.query and self.query.strip()) and not self.identifiers

    @property
    def searches(self) -> bool:
        return self.action == "search" or self.title_download


def _rejection(action: str, message: str) -> dict[str, Any]:
    return {"accepted": False, "action": action,
            "error": FetchError(ErrorCode.INVALID_REQUEST, message).to_dict()}


def request_problem(request: PaperRequest) -> str | None:
    """The first reason this template cannot run, or None. Pure: no network, no filesystem."""
    identifiers, query, directory = request.identifiers, request.query, request.directory
    if request.action not in {"search", "resolve", "download", "batch"}:
        return "action must be search, resolve, download or batch"
    if request.verbosity not in VERBOSITY_LEVELS:
        return "verbosity must be minimal or full"
    if identifiers is not None and (not isinstance(identifiers, list) or
                                    any(not isinstance(item, str) for item in identifiers)):
        return "identifiers must be a list of identifier strings"
    if query is not None and not isinstance(query, str):
        return "query must be text"
    if directory is not None and not isinstance(directory, str):
        return "directory must be a path string"
    if not isinstance(request.review_search_results, bool):
        return "review_search_results must be true or false"
    if request.searches:
        if request.action == "search" and (identifiers or directory or request.concurrency != 4
                                           or request.review_search_results):
            return "search accepts query and search filters, not download settings"
        if request.title_download and request.concurrency != 4:
            return "concurrency applies only to batch"
        if not query or not query.strip():
            return "search requires a nonempty query"
        if request.source not in {"auto", "openalex", "arxiv", "pubmed"}:
            return "source must be auto, openalex, arxiv or pubmed"
        if isinstance(request.limit, bool) or not isinstance(request.limit, int) or not 1 <= request.limit <= 100:
            return "limit must be an integer from 1 to 100"
        if any(year is not None and (isinstance(year, bool) or not isinstance(year, int) or year < 1)
               for year in (request.year_min, request.year_max)
               ) or (request.year_min and request.year_max and request.year_min > request.year_max):
            return "year_min and year_max must be positive years in ascending order"
        return None
    if (query or request.source != "auto" or request.limit != 10
            or request.year_min is not None or request.year_max is not None):
        return "identifier actions do not accept a query or search filters"
    if request.review_search_results:
        return "review_search_results applies only to download by title"
    if not identifiers:
        return f"{request.action} requires identifiers"
    if request.action in {"resolve", "download"} and len(identifiers) != 1:
        return f"{request.action} requires exactly one identifier"
    if request.action == "batch" and not 1 <= len(identifiers) <= 500:
        return "batch requires 1 to 500 identifiers"
    if request.action == "resolve" and (directory or request.concurrency != 4):
        return "resolve does not accept a directory or concurrency"
    if request.action == "download" and request.concurrency != 4:
        return "concurrency applies only to batch"
    if request.action == "batch" and (isinstance(request.concurrency, bool)
                                      or not isinstance(request.concurrency, int)
                                      or not 1 <= request.concurrency <= 16):
        return "concurrency must be an integer from 1 to 16"
    return None


class ServerState:
    def __init__(self, config: Config, http: HttpClient | None = None, registry=None):
        self.config = config
        self.http = http or HttpClient(config)
        self.registry = registry if registry is not None else build_registry(self.http)
        self.crossref = CrossrefProvider(self.http)
        self.europepmc = next((p for p in self.registry if isinstance(p, EuropePmcProvider)), EuropePmcProvider(self.http))
        self.pmc_converter = PmcIdConverter(self.http)
        self._gray_lock = threading.Lock()

    def _permitted(self, candidates: list[PdfCandidate]) -> tuple[list[PdfCandidate], list[dict[str, Any]]]:
        """Split candidates into permitted ones and per-candidate refusal records."""
        allowed: list[PdfCandidate] = []
        errors: list[dict[str, Any]] = []
        for candidate in candidates:
            decision = compliance.evaluate(candidate, allow_gray=self.config.allow_scihub,
                                           license_policy=self.config.license_policy)
            if decision.allowed:
                allowed.append(candidate)
            else:
                errors.append({"provider": candidate.provider,
                               **FetchError(ErrorCode.LICENSE_RESTRICTED,
                                            decision.reason or "candidate refused by licence policy").to_dict()})
        return allowed, errors

    def _enrich(self, record: PaperRecord, prefetched: dict | None = None) -> tuple[PaperRecord, list[dict[str, Any]]]:
        errors: list[dict[str, Any]] = []
        cached_map = (prefetched or {}).get("pmc_id_converter")
        mapped = cached_map.get(record.doi.lower()) if isinstance(cached_map, dict) and record.doi else None
        has_cached_doi = isinstance(cached_map, dict) and record.doi and record.doi.lower() in cached_map
        if mapped:
            record.pmcid = mapped["pmcid"]
            record.pmid = mapped.get("pmid")
        # Europe PMC is the primary DOI/PMID → PMCID mapper, so skip it only when the
        # prefetched id-converter already answered for this DOI. A prefetch that came back
        # empty is not an answer: the id-converter only covers DOIs Europe PMC cannot map.
        if (record.pmid or record.pmcid or record.doi) and not mapped:
            try:
                self.europepmc.enrich(record)
            except FetchError as exc:
                errors.append({"provider": "europepmc", **exc.to_dict()})
        if (record.doi or record.pmid) and not record.pmcid and not has_cached_doi:
            try:
                self.pmc_converter.enrich(record)
            except FetchError as exc:
                errors.append({"provider": "pmc_id_converter", **exc.to_dict()})
        if record.doi and not record.title:
            try:
                self.crossref.enrich(record)
            except FetchError as exc:
                errors.append({"provider": "crossref", **exc.to_dict()})
        return record, errors

    def search(self, query: str, source: str = "auto", limit: int = 10,
               year_min: int | None = None, year_max: int | None = None) -> dict[str, Any]:
        if not query.strip() or not 1 <= limit <= 100 or source not in {"auto", "openalex", "arxiv", "pubmed"}:
            return {"papers": [], "error": FetchError(ErrorCode.BAD_IDENTIFIER, "invalid query, source or limit").to_dict()}
        if year_min and year_max and year_min > year_max:
            return {"papers": [], "error": FetchError(ErrorCode.BAD_IDENTIFIER, "year_min exceeds year_max").to_dict()}
        names = ["openalex", "arxiv", "pubmed"] if source == "auto" else [source]
        results: list[PaperRecord] = []
        errors: list[dict[str, Any]] = []
        for name in names:
            provider = (OpenAlexProvider(self.http) if name == "openalex" else
                        ArxivProvider(self.http) if name == "arxiv" else EuropePmcProvider(self.http))
            try:
                results.extend(provider.search(query, limit, year_min, year_max))
            except FetchError as exc:
                errors.append({"provider": name, **exc.to_dict()})
            except Exception as exc:
                errors.append({"provider": name, **FetchError(ErrorCode.PROVIDER_UNAVAILABLE, str(exc)).to_dict()})
        seen: set[str] = set()
        papers = []
        for record in results:
            key = record.doi.lower() if record.doi else record.best_identifier().lower()
            if key in seen:
                continue
            seen.add(key)
            # Search output must not advertise links the download path would refuse.
            record.pdf_candidates, _ = self._permitted(record.pdf_candidates)
            papers.append(record.to_dict())
            if len(papers) == limit:
                break
        return {"papers": papers, "errors": errors}

    def resolve(self, identifier: str, *, include_gray: bool = False,
                prefetched: dict | None = None) -> dict[str, Any]:
        try:
            record, errors = self._enrich(parse_identifier(identifier))
        except FetchError as exc:
            return {"identifier": identifier, "candidates": [], "error": exc.to_dict()}
        outcome = resolve_candidates(record, self.registry, include_gray=include_gray,
                                     prefetched=prefetched, allow_gray=self.config.allow_scihub,
                                     license_policy=self.config.license_policy)
        candidates, refusals = self._permitted(unique_candidates(record.pdf_candidates + outcome.candidates))
        response = {
            "identifier": record.best_identifier(),
            "paper": record.to_dict(),
            "candidates": [c.to_dict() for c in candidates],
            "errors": errors + outcome.errors + refusals,
        }
        if not candidates:
            response["error"] = _best_error(response["errors"], had_candidate=False)
        return response

    def download(self, identifier: str, directory: str, *, prefetched: dict | None = None) -> dict[str, Any]:
        try:
            record = parse_identifier(identifier)
            destination = _destination(directory, record)
            record, errors = self._enrich(record, prefetched)
        except FetchError as exc:
            return _error_result(identifier, exc)
        if destination.exists() or destination.with_suffix(".pdf.json").exists():
            return FetchResult(ok=False, identifier=identifier, path=str(destination), skipped=True,
                               error=FetchError(ErrorCode.PROVIDER_UNAVAILABLE, "target file already exists").to_dict()).to_dict()
        outcome = resolve_candidates(record, self.registry, prefetched=prefetched,
                                     allow_gray=self.config.allow_scihub,
                                     license_policy=self.config.license_policy)
        errors.extend(outcome.errors)
        candidates, refusals = self._permitted(unique_candidates(record.pdf_candidates + outcome.candidates))
        errors.extend(refusals)
        def attempt(links: list[PdfCandidate]) -> dict[str, Any] | None:
            for candidate in links:
                decision = compliance.evaluate(candidate, allow_gray=self.config.allow_scihub,
                                               license_policy=self.config.license_policy)
                if not decision.allowed:
                    errors.append({"provider": candidate.provider,
                                   **FetchError(ErrorCode.LICENSE_RESTRICTED,
                                                decision.reason or "candidate refused").to_dict()})
                    continue
                try:
                    sha = self.http.download_pdf(candidate, record, destination, decision=decision)
                    return FetchResult(ok=True, identifier=identifier, path=str(destination), sha256=sha,
                                       provider=candidate.provider, compliance_tier=candidate.compliance_tier,
                                       license=decision.license, license_class=decision.license_class,
                                       license_evidence=decision.evidence,
                                       warning="; ".join(decision.warnings) or None).to_dict()
                except FileExistsError:
                    return FetchResult(ok=False, identifier=identifier, path=str(destination), skipped=True,
                                       error=FetchError(ErrorCode.PROVIDER_UNAVAILABLE, "target file already exists").to_dict()).to_dict()
                except FetchError as exc:
                    errors.append({"provider": candidate.provider, **exc.to_dict()})
                except Exception as exc:
                    errors.append({"provider": candidate.provider, **FetchError(ErrorCode.PROVIDER_UNAVAILABLE, str(exc)).to_dict()})
            return None

        if success := attempt(candidates):
            return success
        # A direct PMCID/arXiv link wins the first pass. Only if it fails do we
        # query the aggregator waterfall for alternate legal copies.
        if candidates and (record.pmcid or record.arxiv_id):
            fallback = resolve_candidates(record, self.registry, prefetched=prefetched, skip_specialized=True,
                                          allow_gray=self.config.allow_scihub,
                                          license_policy=self.config.license_policy)
            errors.extend(fallback.errors)
            alternatives = [c for c in unique_candidates(fallback.candidates) if c.url not in {x.url for x in candidates}]
            candidates.extend(alternatives)
            if success := attempt(alternatives):
                return success
        if candidates and record.doi:
            deferred = resolve_candidates(record, self.registry, deferred_only=True,
                                          allow_gray=self.config.allow_scihub,
                                          license_policy=self.config.license_policy)
            errors.extend(deferred.errors)
            alternatives = [c for c in unique_candidates(deferred.candidates)
                            if c.url not in {x.url for x in candidates}]
            candidates.extend(alternatives)
            if success := attempt(alternatives):
                return success
        if self.config.allow_scihub:
            with self._gray_lock:
                for provider in self.registry:
                    if provider.compliance_tier != "gray" or not provider.supports(record):
                        continue
                    try:
                        gray_candidates = provider.find_pdf(record)
                    except FetchError as exc:
                        errors.append({"provider": provider.name, **exc.to_dict()})
                        continue
                    for candidate in gray_candidates:
                        decision = compliance.evaluate(candidate, allow_gray=self.config.allow_scihub,
                                                       license_policy=self.config.license_policy)
                        if not decision.allowed:
                            errors.append({"provider": provider.name,
                                           **FetchError(ErrorCode.LICENSE_RESTRICTED,
                                                        decision.reason or "candidate refused").to_dict()})
                            continue
                        try:
                            sha = self.http.download_pdf(candidate, record, destination, decision=decision)
                            return FetchResult(ok=True, identifier=identifier, path=str(destination), sha256=sha,
                                               provider=candidate.provider, compliance_tier="gray",
                                               license=decision.license, license_class=decision.license_class,
                                               license_evidence=decision.evidence,
                                               warning="; ".join(decision.warnings) or GRAY_WARNING).to_dict()
                        except FetchError as exc:
                            errors.append({"provider": provider.name, **exc.to_dict()})
                        except Exception as exc:
                            errors.append({"provider": provider.name, **FetchError(ErrorCode.PROVIDER_UNAVAILABLE, str(exc)).to_dict()})
        note = ("No PDF found in PMC Article Datasets for this PMCID; a PMC full-text page does not guarantee an available PDF"
                if record.pmcid and record.extra.get("pmc_dataset_pdf") == "not_available" else None)
        return FetchResult(ok=False, identifier=identifier, warning=note,
                           error=_best_error(errors, had_candidate=bool(candidates))).to_dict()

    def download_many(self, identifiers: list[str], directory: str, concurrency: int = 4) -> dict[str, Any]:
        if not 1 <= len(identifiers) <= 500 or not 1 <= concurrency <= 16:
            return {"succeeded": 0, "failed": len(identifiers), "skipped": 0, "items": [],
                    "error": FetchError(ErrorCode.BAD_IDENTIFIER, "batch must have 1-500 identifiers and concurrency 1-16").to_dict()}
        prefetched: dict[str, dict] = {}
        dois = []
        for identifier in identifiers:
            try:
                doi = parse_identifier(identifier).doi
            except FetchError:
                continue
            if doi:
                dois.append(doi)
        unique_dois = list(dict.fromkeys(doi.lower() for doi in dois))
        if unique_dois:
            try:
                prefetched["pmc_id_converter"] = self.pmc_converter.lookup_many(unique_dois)
            except FetchError as exc:
                LOGGER.warning("PMC DOI batch lookup failed; falling back to single lookups: %s",
                               redact_secrets(str(exc)))
        for provider in self.registry:
            if provider.name not in {"openalex", "semantic_scholar"} or not hasattr(provider, "find_pdf_batch") or not unique_dois:
                continue
            try:
                prefetched[provider.name] = provider.find_pdf_batch(unique_dois)
            except Exception as exc:
                LOGGER.warning("%s batch lookup failed; falling back to single lookups: %s",
                               provider.name, redact_secrets(str(exc)))
        results: list[dict[str, Any] | None] = [None] * len(identifiers)
        seen: set[str] = set()
        todo = []
        for index, identifier in enumerate(identifiers):
            try:
                parsed = parse_identifier(identifier)
                key = parsed.best_identifier().lower()
                bucket = (0 if parsed.pmcid else 1 if parsed.arxiv_id else 2 if parsed.pmid else
                          3 if parsed.extra else 4 if parsed.doi else 5)
            except FetchError:
                key = f"invalid:{index}"
                bucket = 6
            if key in seen:
                results[index] = FetchResult(ok=False, identifier=identifier, skipped=True,
                                              error=FetchError(ErrorCode.BAD_IDENTIFIER, "duplicate identifier in batch").to_dict()).to_dict()
            else:
                seen.add(key)
                todo.append((bucket, index, identifier))
        todo.sort()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [(index, identifier, pool.submit(self.download, identifier, directory, prefetched=prefetched))
                       for _, index, identifier in todo]
            for index, identifier, future in futures:
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = _error_result(identifier, FetchError(ErrorCode.PROVIDER_UNAVAILABLE, str(exc)))
        items = [item for item in results if item is not None]
        return {"succeeded": sum(item["ok"] for item in items),
                "failed": sum(not item["ok"] and not item["skipped"] for item in items),
                "skipped": sum(item["skipped"] for item in items), "items": items}

    def execute_request(self, action: str, *, identifiers: list[str] | None = None,
                        query: str | None = None, directory: str | None = None,
                        source: str = "auto", limit: int = 10,
                        year_min: int | None = None, year_max: int | None = None,
                        concurrency: int = 4, verbosity: str = "minimal",
                        review_search_results: bool = False) -> dict[str, Any]:
        """Validate a skill-normalized request, run it, and shape the result for the model."""
        request = PaperRequest(action=action, identifiers=identifiers, query=query, directory=directory,
                               source=source, limit=limit, year_min=year_min, year_max=year_max,
                               concurrency=concurrency, verbosity=verbosity,
                               review_search_results=review_search_results)
        if problem := request_problem(request):
            return _rejection(action, problem)
        return self._run_request(request)

    def _run_request(self, request: PaperRequest) -> dict[str, Any]:
        action, verbosity = request.action, request.verbosity
        if request.searches:
            found = self.search(request.query.strip(), request.source, request.limit,
                                request.year_min, request.year_max)
            shaped = _search_summary(found) if verbosity == "minimal" else found
            if action == "search":
                return {"accepted": True, "action": action, "result": shaped}
            destination, rejection = self._download_target(request)
            if rejection:
                return rejection
            title_key = re.sub(r"[^\w]+", "", request.query.casefold())
            if not title_key:
                return _rejection(action, "query must contain title text")
            matches: dict[str, dict[str, Any]] = {}
            for paper in found.get("papers", []):
                if re.sub(r"[^\w]+", "", (paper.get("title") or "").casefold()) != title_key:
                    continue
                identifier = paper.get("doi") or paper.get("pmcid") or paper.get("arxiv_id")
                if identifier:
                    try:
                        matches[parse_identifier(identifier).best_identifier().lower()] = paper
                    except FetchError:
                        continue
            if request.review_search_results or len(matches) != 1:
                return {"accepted": True, "action": action, "needs_review": True, "result": shaped,
                        "reason": "review requested" if request.review_search_results else
                                  "no unique exact title match"}
            identifier, paper = next(iter(matches.items()))
            return {"accepted": True, "action": action, "needs_review": False,
                    "matched_title": paper["title"], "result": self.download(identifier, destination)}

        try:
            normalized = [parse_identifier(item).best_identifier() for item in request.identifiers]
        except FetchError as exc:
            return _rejection(action, str(exc))
        if action == "resolve":
            resolved = self.resolve(normalized[0], include_gray=self.config.allow_scihub)
            return {"accepted": True, "action": action,
                    "result": _resolve_summary(resolved) if verbosity == "minimal" else resolved}
        destination, rejection = self._download_target(request)
        if rejection:
            return rejection
        if action == "batch":
            result = self.download_many(normalized, destination, request.concurrency)
            if verbosity == "minimal":
                result = _batch_summary(result, destination)
        else:
            result = self.download(normalized[0], destination)
        return {"accepted": True, "action": action, "result": result}

    def _download_target(self, request: PaperRequest) -> tuple[str | None, dict[str, Any] | None]:
        """The resolved destination directory, or a ready-made rejection reply."""
        destination = request.directory.strip() if request.directory else self.config.download_directory
        if not destination:
            return None, _rejection(request.action,
                                    "directory is required; set it in the request or configure download_directory")
        target = _resolve_directory(destination)
        if target is None:
            return None, _rejection(request.action, "directory must be a usable local path")
        return target, None

    def status(self) -> dict[str, Any]:
        return {"providers": [{"name": p.name, "priority": p.priority, "compliance_tier": p.compliance_tier,
                               "enabled": p.name != "core" or bool(self.config.core_api_key)} for p in self.registry],
                "metadata_sources": ["crossref", "europepmc", "pmc_id_converter"],
                "core_configured": bool(self.config.core_api_key),
                "scihub_enabled": self.config.allow_scihub,
                "compliance": {"license_policy": self.config.license_policy,
                               "policy_version": compliance.POLICY_VERSION,
                               "gray_sources_enabled": self.config.allow_scihub,
                               "license_required_by_default": True,
                               "source_attested_providers": sorted(compliance.SOURCE_ATTESTED_PROVIDERS)},
                "hosts": self.http.limiter.snapshot()}


def get_state() -> ServerState:
    global _state
    if _state is None:
        _state = ServerState(Config.from_file())
    return _state


@mcp.tool
def get_server_status() -> dict[str, Any]:
    """Report registered sources and current host throttling state without credentials."""
    return get_state().status()


@mcp.tool
def execute_paper_request(action: Literal["search", "resolve", "download", "batch"],
                          identifiers: list[str] | None = None, query: str | None = None,
                          directory: str | None = None, source: str = "auto", limit: int = 10,
                          year_min: int | None = None, year_max: int | None = None,
                          concurrency: int = 4, verbosity: Literal["minimal", "full"] = "minimal",
                          review_search_results: bool = False) -> dict[str, Any]:
    """Execute a paper-fetch skill template filled from natural language. Download by exact
    title using query, without review by default; ambiguous matches return needs_review.
    Downloads use the configured download_directory when directory is omitted.
    """
    return get_state().execute_request(action, identifiers=identifiers, query=query, directory=directory,
                                       source=source, limit=limit, year_min=year_min, year_max=year_max,
                                       concurrency=concurrency, verbosity=verbosity,
                                       review_search_results=review_search_results)


def main(argv: list[str] | None = None) -> None:
    global _state
    parser = argparse.ArgumentParser(description="Scholarly PDF MCP service")
    parser.add_argument("--config", help="TOML config path (default: paper-fetch.toml in the working directory)")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    try:
        _state = ServerState(Config.from_file(args.config))
    except ConfigError as exc:
        LOGGER.error("configuration error: %s", exc)
        raise SystemExit(2) from exc
    policy = _state.config.license_policy
    LOGGER.info("Licence policy: %s (policy v%s). Gray sources: %s.",
                policy, compliance.POLICY_VERSION,
                "ENABLED" if _state.config.allow_scihub else "disabled")
    if _state.config.allow_scihub:
        LOGGER.warning(
            "allow_scihub = true: gray mirrors redistribute in-copyright works without a licence. "
            "Downloads from them are marked compliance_tier=gray and are your legal responsibility.")
    if policy == "any":
        LOGGER.warning(
            "license_policy = any: candidates without licence evidence will be downloaded. "
            "This disables the copyright-safety boundary; confirm this is intended.")
    LOGGER.info("Use scholarly sources in accordance with local law and institutional policy.")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
