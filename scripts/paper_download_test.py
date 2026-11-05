"""Live literature-download test through the skill entry `execute_request`.

Covers search / resolve / download / batch against real providers, then verifies every
produced PDF independently: %PDF header, SHA-256 against the returned hash and the JSON
sidecar, page count via pypdf, and the resolved paper title appearing in the extracted
text of the first pages.

The `audit` stage is the copyright-safety check and runs entirely offline: it proves
that a candidate without licence evidence is refused, that gray sources are off, and
that the same refusal is reported as LICENSE_RESTRICTED rather than as a paywall. The
live stages run with the default policy, so any PDF they produce carries a recorded
licence or an attesting source in its sidecar.

Usage:
    .venv\\Scripts\\python.exe scripts\\paper_download_test.py                 # all stages
    .venv\\Scripts\\python.exe scripts\\paper_download_test.py search download # selected stages
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pypdf import PdfReader

from paper_fetch_mcp_safe import compliance
from paper_fetch_mcp_safe.config import Config
from paper_fetch_mcp_safe.http import HttpClient
from paper_fetch_mcp_safe.identifiers import parse_identifier
from paper_fetch_mcp_safe.models import PdfCandidate
from paper_fetch_mcp_safe.ratelimit import RateLimiter
from paper_fetch_mcp_safe.server import ServerState

ROOT = Path.cwd() / ".tmp" / "lit-download-test"
SINGLE = [
    ("europepmc_doi", "10.1021/jacs.4c09998", "europepmc", "publisher_oa"),
    ("arxiv_direct", "arxiv:1706.03762", "arxiv", "preprint"),
]
BATCH = [
    ("pmc_cloud", "PMC10009416", "europepmc"),
    ("hal", "hal:hal-00721635", "hal"),
    ("zenodo", "zenodo:1062358", "zenodo"),
    ("osf_preprint", "osf:hqb2p_v1", "osf_preprints"),
    ("openalex_wiley_oa", "10.1002/anie.202403417", "openalex"),
]
CLOSED = [
    ("angew_standard", "10.1002/anie.201900650"),
    ("jpca_standard", "10.1021/acs.jpca.4c03416"),
]
SEARCH = ("water oxidation catalyst", 5)
STAGES = ("search", "resolve", "download", "batch", "audit")


def title_probe(title: str | None, identifier: str) -> str | None:
    if title:
        tokens = [t for t in re.findall(r"[A-Za-z]{5,}", title)][:12]
        if tokens:
            return " ".join(tokens)
    return identifier.split(":")[-1] if identifier else None


def verify(state: ServerState, download: dict, identifier: str) -> dict:
    """Independent artifact check: header, hash chain, parseability, title text."""
    checks: dict[str, object] = {}
    if not download.get("ok"):
        return checks
    pdf = Path(download["path"])
    sidecar = Path(str(pdf) + ".json")
    content = pdf.read_bytes()
    checks["path"] = str(pdf)
    checks["bytes"] = pdf.stat().st_size
    checks["pdf_magic"] = content.startswith(b"%PDF")
    file_hash = hashlib.sha256(content).hexdigest()
    meta = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    checks["sidecar_present"] = sidecar.exists()
    checks["sha256_match"] = file_hash == download.get("sha256") == meta.get("sha256")
    checks["sidecar_license"] = meta.get("license")
    checks["license_evidence"] = meta.get("license_evidence")
    checks["license_class"] = meta.get("license_class")
    checks["sidecar_url_host"] = urlparse(meta.get("url", "")).hostname
    try:
        reader = PdfReader(str(pdf))
        checks["pages"] = len(reader.pages)
        pages = min(3, len(reader.pages))
        head = "\n".join((reader.pages[i].extract_text() or "") for i in range(pages))
        resolved = state.resolve(identifier)
        title = (resolved.get("paper") or {}).get("title")
        checks["resolved_title"] = title
        probe = title_probe(title, identifier)
        if probe:
            tokens = [t.lower() for t in probe.split()]
            hits = sum(1 for t in tokens if t in head.lower())
            checks["title_token_hits"] = f"{hits}/{len(tokens)}"
            checks["title_in_text"] = hits >= max(1, len(tokens) // 2)
    except Exception as exc:  # corrupt/unparseable PDFs must surface, not pass silently
        checks["parse_error"] = f"{type(exc).__name__}: {exc}"
    checks["verified"] = all([
        checks["pdf_magic"], checks["sidecar_present"], checks["sha256_match"],
        checks.get("pages", 0) > 0, checks.get("title_in_text", False),
        # Every downloaded file must say how its licence was established.
        checks["license_evidence"] in {"license", "source-attested", "oa-declared"},
    ])
    return checks


def summarize(download: dict) -> dict:
    error = download.get("error") or {}
    return {
        "ok": download.get("ok", False),
        "provider": download.get("provider"),
        "compliance_tier": download.get("compliance_tier"),
        "skipped": download.get("skipped", False),
        "warning": download.get("warning"),
        "error_code": error.get("code"),
        "error_message": error.get("message"),
    }


def run_audit(state: ServerState) -> dict:
    """Offline proof of the copyright-safety boundary; no provider is contacted."""
    import tempfile

    def client(handler) -> HttpClient:
        config = Config(mailto=state.config.mailto)
        return HttpClient(config, RateLimiter(rates={"cdn.example": 100}),
                          httpx.Client(transport=httpx.MockTransport(handler)))

    unlicensed = client(lambda _: (_ for _ in ()).throw(AssertionError("must not be fetched")))
    refused = []
    with tempfile.TemporaryDirectory() as directory:
        try:
            unlicensed.download_pdf(PdfCandidate(url="https://cdn.example/paid.pdf", provider="openalex"),
                                    parse_identifier("10.1234/paid"), Path(directory) / "paid.pdf")
        except Exception as exc:  # recorded, then asserted below
            refused.append({"error_code": getattr(exc, "code", None) and exc.code.value,
                            "message": str(exc),
                            "files_written": len(list(Path(directory).iterdir()))})
    grey = compliance.evaluate(PdfCandidate(url="https://cdn.example/x.pdf", provider="scihub",
                                            compliance_tier="gray"))
    audit = {
        "policy_version": compliance.POLICY_VERSION,
        "license_policy": state.config.license_policy,
        "gray_sources_enabled": state.config.allow_scihub,
        "unlicensed_candidate": refused[0] if refused else {"error_code": None},
        "gray_candidate_refused": not grey.allowed,
        "registry_has_gray_provider": any(p.compliance_tier == "gray" for p in state.registry),
        "sidecar_fields": ["license", "license_normalized", "license_class",
                           "license_evidence", "license_policy", "policy_version"],
    }
    audit["passed"] = (
        audit["unlicensed_candidate"].get("error_code") == "LICENSE_RESTRICTED"
        and audit["unlicensed_candidate"].get("files_written") == 0
        and audit["gray_candidate_refused"]
        and not audit["registry_has_gray_provider"]
        and not audit["gray_sources_enabled"]
    )
    print(f"[audit] boundary_holds={audit['passed']} "
          f"unlicensed={audit['unlicensed_candidate'].get('error_code')} "
          f"gray_refused={audit['gray_candidate_refused']}", flush=True)
    return audit


def main() -> None:
    stages = set(sys.argv[1:]) or set(STAGES)
    unknown = stages - set(STAGES)
    if unknown:
        raise SystemExit(f"unknown stages: {', '.join(sorted(unknown))}")

    base = Config.from_file()
    oa_dir = ROOT / "pdfs"
    batch_dir = ROOT / "batch"
    for directory in (oa_dir, batch_dir):
        directory.mkdir(parents=True, exist_ok=True)
    state = ServerState(replace(base, allow_scihub=False))
    report: dict[str, object] = {
        "date": time.strftime("%Y-%m-%d"),
        "actions_tested": sorted(stages),
        "license_policy": state.config.license_policy,
        "oa_providers": [p["name"] for p in state.status()["providers"] if p["enabled"]],
        "gray_providers": [p["name"] for p in state.status()["providers"]
                           if p["compliance_tier"] == "gray"],
        "stages": {},
    }
    try:
        if "search" in stages:
            query, limit = SEARCH
            started = time.monotonic()
            response = state.execute_request("search", query=query, limit=limit, verbosity="full")
            papers = (response.get("result") or {}).get("papers", [])
            report["stages"]["search"] = {
                "accepted": response.get("accepted"), "query": query,
                "papers": [{"title": p.get("title"), "year": p.get("year"), "doi": p.get("doi"),
                            "candidates": len(p.get("pdf_candidates", []))} for p in papers],
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
            print(f"[search] {len(papers)} papers for {query!r}", flush=True)

        if "resolve" in stages:
            items = []
            for label, identifier in CLOSED:
                started = time.monotonic()
                result = (state.execute_request("resolve", identifiers=[identifier],
                                                verbosity="full").get("result") or {})
                code = (result.get("error") or {}).get("code")
                items.append({"label": label, "identifier": identifier,
                              "candidate_count": len(result.get("candidates", [])), "error_code": code,
                              "explicit_no_oa": code == "PAYWALL_NO_OA",
                              "elapsed_seconds": round(time.monotonic() - started, 2)})
                print(f"[resolve] {identifier} -> {code}", flush=True)
            report["stages"]["resolve"] = {"items": items}

        if "download" in stages:
            items = []
            for label, identifier, provider, access in SINGLE:
                started = time.monotonic()
                download = (state.execute_request("download", identifiers=[identifier],
                                                  directory=str(oa_dir)).get("result") or {})
                item = {"label": label, "identifier": identifier, "expect_provider": provider,
                        "access": access, **summarize(download)}
                item["verification"] = verify(state, download, identifier)
                item["elapsed_seconds"] = round(time.monotonic() - started, 2)
                items.append(item)
                print(f"[download] {label} {identifier} -> ok={item['ok']} provider={item['provider']} "
                      f"verified={item['verification'].get('verified')}", flush=True)
            report["stages"]["download"] = {"directory": str(oa_dir), "items": items}

        if "batch" in stages:
            started = time.monotonic()
            identifiers = [identifier for _, identifier, _ in BATCH]
            result = (state.execute_request("batch", identifiers=identifiers, directory=str(batch_dir),
                                            concurrency=4, verbosity="full").get("result") or {})
            items = []
            for label, identifier, provider in BATCH:
                wanted = parse_identifier(identifier).best_identifier().lower()
                raw = [entry for entry in result.get("items", [])
                       if parse_identifier(entry.get("identifier", "")).best_identifier().lower() == wanted]
                download = raw[0] if raw else {}
                item = {"label": label, "identifier": identifier, "expect_provider": provider,
                        "access": "repository", "item_found_in_response": bool(raw),
                        **summarize(download)}
                item["verification"] = verify(state, download, identifier)
                items.append(item)
                print(f"[batch] {label} {identifier} -> ok={item['ok']} provider={item['provider']} "
                      f"verified={item['verification'].get('verified')}", flush=True)
            report["stages"]["batch"] = {
                "directory": str(batch_dir), "server_succeeded": result.get("succeeded"),
                "server_failed": result.get("failed"), "server_skipped": result.get("skipped"),
                "raw_items": result.get("items"), "items": items,
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }

        if "audit" in stages:
            report["stages"]["audit"] = run_audit(state)

    finally:
        state.http.close()

    out = ROOT / "results.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport -> {out}")


if __name__ == "__main__":
    main()
