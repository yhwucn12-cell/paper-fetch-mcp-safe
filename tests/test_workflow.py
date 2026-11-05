"""The service contract and fallback behavior without external requests."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from fastmcp import Client

from paper_fetch_mcp_safe.config import Config
from paper_fetch_mcp_safe.http import HttpClient
from paper_fetch_mcp_safe.models import ErrorCode, FetchError, PdfCandidate
from paper_fetch_mcp_safe.ratelimit import RateLimiter
from paper_fetch_mcp_safe.router import build_registry
from paper_fetch_mcp_safe.server import PaperRequest, ServerState, mcp, request_problem
import paper_fetch_mcp_safe.server as server_module


class FixedProvider:
    compliance_tier = "oa"

    def __init__(self, name, priority, links, calls):
        self.name, self.priority, self.links, self.calls = name, priority, links, calls

    def supports(self, record):
        return bool(record.doi or record.arxiv_id)

    def find_pdf(self, record):
        self.calls.append(self.name)
        return [PdfCandidate(url=url, provider=self.name, license="cc-by") for url in self.links]


def mock_state(handler, providers):
    config = Config(mailto="test@example.org")
    http = HttpClient(config, RateLimiter(rates={"bad.example": 100, "good.example": 100}),
                      httpx.Client(transport=httpx.MockTransport(handler)))
    state = ServerState(config, http, providers)
    state.crossref.enrich = lambda record: record
    return state


def test_only_the_structured_entry_and_status_are_exposed():
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == {"execute_paper_request", "get_server_status"}
    by_name = {tool.name: tool for tool in tools}
    assert {"action", "identifiers", "query", "directory", "verbosity"} <= set(
        by_name["execute_paper_request"].parameters["properties"])
    assert by_name["execute_paper_request"].parameters["properties"]["verbosity"]["default"] == "minimal"


def test_structured_request_through_mcp_client(monkeypatch):
    state = mock_state(lambda _: httpx.Response(404), [])
    state.resolve = lambda identifier, *, include_gray=False: {"identifier": identifier, "candidates": []}
    monkeypatch.setattr(server_module, "_state", state)

    async def check():
        async with Client(mcp) as client:
            response = await client.call_tool("execute_paper_request", {
                "action": "resolve", "identifiers": ["https://doi.org/10.1234/a"]})
            assert not response.is_error
            assert response.data["accepted"]
            assert response.data["result"]["identifier"] == "doi:10.1234/a"

    asyncio.run(check())


def test_structured_request_dispatch_and_default_directory(tmp_path):
    calls = []
    state = mock_state(lambda _: httpx.Response(200, content=b"%PDF-1.7\nvalid"),
                       [FixedProvider("oa", 10, ["https://good.example/paper.pdf"], calls)])
    state.config.download_directory = str(tmp_path)
    state.search = lambda query, source, limit, year_min, year_max: {"papers": [{"title": query}], "errors": []}
    found = state.execute_request("search", query="  water oxidation  ", limit=3)
    assert found["accepted"] and "request" not in found
    assert found["result"]["papers"][0]["title"] == "water oxidation"
    saved = state.execute_request("download", identifiers=["https://doi.org/10.1234/a"])
    assert saved["accepted"] and saved["action"] == "download" and "request" not in saved
    assert saved["result"]["ok"] and next(tmp_path.glob("*.pdf")).exists()
    sidecar = json.loads(next(tmp_path.glob("*.pdf.json")).read_text(encoding="utf-8"))
    assert sidecar["identifier"] == "doi:10.1234/a"


def test_structured_request_rejects_ambiguous_or_incomplete_input(tmp_path):
    state = mock_state(lambda _: httpx.Response(200, content=b"%PDF-1.7\nvalid"), [])
    for kwargs in (
        {"action": "download", "query": "A paper title"},
        {"action": "download", "identifiers": ["10.1234/a"]},
        {"action": "batch", "identifiers": ["10.1234/a", "a paper title"], "directory": str(tmp_path)},
        {"action": "resolve", "identifiers": ["10.1234/a", "10.1234/b"]},
    ):
        result = state.execute_request(**kwargs)
        assert not result["accepted"]
        assert result["error"]["code"] == ErrorCode.INVALID_REQUEST
    assert list(tmp_path.iterdir()) == []


def test_title_download_auto_selects_exact_match_unless_review_requested(tmp_path):
    state = mock_state(lambda _: httpx.Response(200, content=b"%PDF-1.7\nvalid"), [])
    state.config.download_directory = str(tmp_path)
    state.search = lambda *_: {"papers": [
        {"title": "Another Water Oxidation Study", "doi": "10.1234/other"},
        {"title": "Water Oxidation: A Study", "doi": "10.1234/exact"}], "errors": []}
    downloaded = []
    state.download = lambda identifier, directory: downloaded.append((identifier, directory)) or {"ok": True}
    result = state.execute_request("download", query="Water Oxidation - A Study")
    assert result["accepted"] and not result["needs_review"]
    assert result["matched_title"] == "Water Oxidation: A Study"
    assert downloaded == [("doi:10.1234/exact", str(tmp_path))]
    reviewed = state.execute_request("download", query="Water Oxidation: A Study", review_search_results=True)
    assert reviewed["accepted"] and reviewed["needs_review"]
    assert len(downloaded) == 1
    state.search = lambda *_: {"papers": [
        {"title": "Water Oxidation: A Study", "doi": "10.1234/one"},
        {"title": "Water Oxidation: A Study", "doi": "10.1234/two"}], "errors": []}
    ambiguous = state.execute_request("download", query="Water Oxidation: A Study")
    assert ambiguous["accepted"] and ambiguous["needs_review"]
    assert len(downloaded) == 1


def test_default_and_opt_in_registry():
    def make(enabled):
        config = Config(mailto="test@example.org", allow_scihub=enabled)
        http = HttpClient(config, client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))))
        return [provider.name for provider in build_registry(http)]
    assert "scihub" not in make(False)
    assert make(True)[-1] == "scihub"
    # The safe build ships with gray sources off unless the operator opts in.
    assert Config(mailto="test@example.org").allow_scihub is False


def test_candidate_failure_falls_back_and_sidecar(tmp_path):
    calls = []
    def handle(req):
        if req.url.host == "bad.example":
            return httpx.Response(200, content=b"not a PDF")
        return httpx.Response(200, content=b"%PDF-1.7\nvalid")
    providers = [FixedProvider("first", 10, ["https://bad.example/a"], calls),
                 FixedProvider("second", 20, ["https://good.example/b"], calls)]
    state = mock_state(handle, providers)
    result = state.download("10.1234/a", str(tmp_path))
    assert result["ok"] and result["provider"] == "second"
    assert calls == ["first", "second"]
    assert hashlib.sha256((tmp_path / next(p.name for p in tmp_path.glob("*.pdf"))).read_bytes()).hexdigest() == result["sha256"]
    sidecar = json.loads((tmp_path / (next(p.name for p in tmp_path.glob("*.pdf")) + ".json")).read_text())
    assert sidecar["provider"] == "second"
    assert state.download("10.1234/a", str(tmp_path))["skipped"]


def test_specialized_short_circuit_then_oa_fallback(tmp_path):
    calls = []
    def handle(req):
        return httpx.Response(200, content=b"broken" if req.url.host == "bad.example" else b"%PDF-good")
    providers = [FixedProvider("special", 6, ["https://bad.example/a"], calls),
                 FixedProvider("openalex", 10, ["https://good.example/b"], calls)]
    state = mock_state(handle, providers)
    result = state.download("arxiv:1706.03762", str(tmp_path))
    assert result["ok"] and result["provider"] == "openalex"
    assert calls == ["special", "openalex"]


def test_batch_partial_failure_duplicate_and_prefetch(tmp_path):
    calls = []
    providers = [FixedProvider("oa", 10, ["https://good.example/a"], calls)]
    state = mock_state(lambda _: httpx.Response(200, content=b"%PDF-1.5\ntest"), providers)
    result = state.download_many(["10.1234/a", "bad-id", "https://doi.org/10.1234/a"], str(tmp_path), 2)
    assert (result["succeeded"], result["failed"], result["skipped"]) == (1, 1, 1)
    assert result["items"][1]["error"]["code"] == ErrorCode.BAD_IDENTIFIER
    assert "duplicate" in result["items"][2]["error"]["message"]


def test_error_distinguishes_empty_from_unavailable(tmp_path):
    calls = []
    state = mock_state(lambda _: httpx.Response(404), [FixedProvider("empty", 10, [], calls)])
    assert state.download("10.1234/a", str(tmp_path))["error"]["code"] == ErrorCode.PAYWALL_NO_OA
    class Broken(FixedProvider):
        def find_pdf(self, record):
            raise FetchError(ErrorCode.RATE_LIMITED, "slow down", 20)
    state.registry = [Broken("broken", 10, [], calls)]
    error = state.download("10.1234/b", str(tmp_path))["error"]
    assert error["code"] == ErrorCode.RATE_LIMITED and error["retry_after"] == 20


def test_gray_source_only_after_oa_fails_and_is_disclosed(tmp_path):
    calls = []
    class Gray(FixedProvider):
        compliance_tier = "gray"
        def find_pdf(self, record):
            self.calls.append(self.name)
            return [PdfCandidate(url=url, provider=self.name, compliance_tier="gray") for url in self.links]
    providers = [FixedProvider("oa", 10, ["https://bad.example/a"], calls),
                 Gray("scihub", 100, ["https://good.example/b"], calls)]
    config = Config(mailto="test@example.org", allow_scihub=True)
    http = HttpClient(config, RateLimiter(rates={"bad.example": 100, "good.example": 100}),
                      httpx.Client(transport=httpx.MockTransport(
                          lambda req: httpx.Response(200, content=b"%PDF-ok" if req.url.host == "good.example" else b"html"))))
    state = ServerState(config, http, providers)
    state.crossref.enrich = lambda record: record
    result = state.download("10.1234/a", str(tmp_path))
    assert result["ok"] and result["compliance_tier"] == "gray" and result["warning"]
    assert calls == ["oa", "scihub"]
    pdf = next(tmp_path.glob("*.pdf"))
    assert json.loads((tmp_path / (pdf.name + ".json")).read_text())["compliance_tier"] == "gray"


def test_search_returns_serializable_paper_with_candidates():
    body = {"results": [{"title": "An OA Paper", "doi": "https://doi.org/10.1234/a",
                         "publication_year": 2022, "open_access": {"is_oa": True, "oa_status": "gold"},
                         "best_oa_location": {"pdf_url": "https://good.example/a.pdf", "is_oa": True}}]}
    state = mock_state(lambda _: httpx.Response(200, json=body), [])
    result = state.search("paper", source="openalex", year_min=2020, year_max=2024)
    assert result["papers"][0]["doi"] == "10.1234/a"
    assert result["papers"][0]["pdf_candidates"][0]["provider"] == "openalex"
    json.dumps(result)


def test_batch_buckets_special_identifiers_before_generic_dois(tmp_path):
    state = mock_state(lambda _: httpx.Response(200, content=b"%PDF-test"), [])
    observed = []
    def download(identifier, directory, *, prefetched=None):
        observed.append(identifier)
        return {"ok": True, "identifier": identifier, "skipped": False}
    state.download = download
    state.download_many(["10.1234/a", "arxiv:1706.03762", "PMC12345", "12345"], str(tmp_path), 1)
    assert observed == ["PMC12345", "arxiv:1706.03762", "12345", "10.1234/a"]


def test_interrupted_download_tries_next_candidate(tmp_path):
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF-beginning"
            raise httpx.ReadError("connection lost")
    calls = []
    def handler(req):
        if req.url.host == "bad.example":
            return httpx.Response(200, stream=BrokenStream())
        return httpx.Response(200, content=b"%PDF-complete")
    state = mock_state(handler, [FixedProvider("first", 10, ["https://bad.example/a"], calls),
                                 FixedProvider("second", 20, ["https://good.example/b"], calls)])
    result = state.download("10.1234/a", str(tmp_path))
    assert result["ok"] and result["provider"] == "second"
    assert len(list(tmp_path.iterdir())) == 2


def test_repository_lookups_defer_until_earlier_pdf_fails(tmp_path):
    calls = []
    def handler(req):
        return httpx.Response(200, content=b"%PDF-repository" if req.url.host == "good.example" else b"HTML")
    providers = [FixedProvider("openalex", 10, ["https://bad.example/paper"], calls),
                 FixedProvider("hal", 12, ["https://good.example/paper"], calls),
                 FixedProvider("zenodo", 15, [], calls)]
    state = mock_state(handler, providers)
    state.europepmc.enrich = lambda record: record
    state.pmc_converter.enrich = lambda record: record
    resolved = state.resolve("10.1234/a")
    assert [item["provider"] for item in resolved["candidates"]] == ["openalex"]
    assert calls == ["openalex"]
    calls.clear()
    result = state.download("10.1234/a", str(tmp_path))
    assert result["ok"] and result["provider"] == "hal"
    assert calls == ["openalex", "hal", "zenodo"]


def test_minimal_search_rows_carry_only_pick_fields():
    body = {"results": [{"title": "An OA Paper", "doi": "https://doi.org/10.1234/a",
                         "publication_year": 2022, "open_access": {"is_oa": True, "oa_status": "gold"},
                         "best_oa_location": {"pdf_url": "https://good.example/a.pdf", "is_oa": True}}]}
    state = mock_state(lambda _: httpx.Response(200, json=body), [])
    minimal = state.execute_request("search", query="paper", source="openalex")
    assert minimal["result"]["papers"] == [
        {"title": "An OA Paper", "year": 2022, "doi": "10.1234/a", "id": "doi:10.1234/a"}]
    full = state.execute_request("search", query="paper", source="openalex", verbosity="full")
    assert full["result"]["papers"][0]["pdf_candidates"][0]["url"] == "https://good.example/a.pdf"


def test_minimal_search_id_follows_record_precedence():
    state = mock_state(lambda _: httpx.Response(404), [])
    state.search = lambda *_: {"papers": [{"title": "T", "year": 2020, "doi": "10.1/a",
                                           "pmcid": "PMC1", "arxiv_id": "2401.00001", "pmid": "9"}],
                               "errors": []}
    row = state.execute_request("search", query="T")["result"]["papers"][0]
    assert row["id"] == "pmcid:PMC1"


def test_minimal_resolve_reports_providers_without_urls():
    state = mock_state(lambda _: httpx.Response(200), [])
    state.resolve = lambda identifier, *, include_gray=False: {
        "identifier": "doi:10.1234/a", "paper": {"title": "A Paper"},
        "candidates": [{"url": "https://good.example/a.pdf", "provider": "openalex",
                        "compliance_tier": "oa", "license": "cc-by"}], "errors": []}
    minimal = state.execute_request("resolve", identifiers=["10.1234/a"])
    assert minimal["result"] == {"identifier": "doi:10.1234/a", "title": "A Paper", "candidate_count": 1,
                                 "candidates": [{"provider": "openalex", "compliance_tier": "oa",
                                                 "license_class": "open"}]}
    assert "https://" not in json.dumps(minimal)
    full = state.execute_request("resolve", identifiers=["10.1234/a"], verbosity="full")
    assert full["result"]["candidates"][0]["url"] == "https://good.example/a.pdf"


def _batch_stub(identifiers, directory, concurrency=4):
    return {"succeeded": 1, "failed": 1, "skipped": 0, "items": [
        {"ok": True, "identifier": "doi:10.1234/a", "path": "a.pdf"},
        {"ok": False, "identifier": "doi:10.1234/b", "path": None,
         "error": {"code": "PAYWALL_NO_OA", "message": "no registered source returned an OA PDF"}}]}


def test_minimal_batch_writes_results_file(tmp_path):
    state = mock_state(lambda _: httpx.Response(200), [])
    state.download_many = _batch_stub
    summary = state.execute_request("batch", identifiers=["10.1234/a", "10.1234/b"],
                                    directory=str(tmp_path))["result"]
    assert (summary["succeeded"], summary["failed"], summary["skipped"]) == (1, 1, 0)
    assert summary["failures"] == [{"identifier": "doi:10.1234/b", "code": "PAYWALL_NO_OA"}]
    assert "items" not in summary
    written = json.loads(Path(summary["results_path"]).read_text(encoding="utf-8"))
    assert [item["identifier"] for item in written["items"]] == ["doi:10.1234/a", "doi:10.1234/b"]
    assert written["directory"] == str(tmp_path)


def test_minimal_batch_returns_items_when_results_file_is_unwritable(tmp_path):
    state = mock_state(lambda _: httpx.Response(200), [])
    state.download_many = _batch_stub
    summary = state.execute_request("batch", identifiers=["10.1234/a", "10.1234/b"],
                                    directory=str(tmp_path / "missing"))["result"]
    assert len(summary["items"]) == 2 and "results_path" not in summary
    assert "note" in summary


def test_unknown_verbosity_is_rejected():
    state = mock_state(lambda _: httpx.Response(200), [])
    result = state.execute_request("search", query="paper", verbosity="brief")
    assert not result["accepted"] and result["action"] == "search"
    assert result["error"]["code"] == ErrorCode.INVALID_REQUEST
    assert "verbosity" in result["error"]["message"]


def test_minimal_batch_discloses_gray_source(tmp_path):
    state = mock_state(lambda _: httpx.Response(200), [])
    state.download_many = lambda identifiers, directory, concurrency=4: {
        "succeeded": 1, "failed": 0, "skipped": 0,
        "items": [{"ok": True, "identifier": "doi:10.1234/a", "compliance_tier": "gray",
                   "warning": "Gray source: review local law and institutional policy"}]}
    summary = state.execute_request("batch", identifiers=["10.1234/a"], directory=str(tmp_path))["result"]
    assert summary["gray"] == 1 and "Gray source" in summary["warning"]


def test_batch_results_files_do_not_collide(tmp_path):
    state = mock_state(lambda _: httpx.Response(200), [])
    state.download_many = _batch_stub
    first = state.execute_request("batch", identifiers=["10.1234/a", "10.1234/b"],
                                  directory=str(tmp_path))["result"]
    second = state.execute_request("batch", identifiers=["10.1234/a", "10.1234/b"],
                                   directory=str(tmp_path))["result"]
    assert first["results_path"] != second["results_path"]
    assert Path(first["results_path"]).exists() and Path(second["results_path"]).exists()
    assert not list(tmp_path.glob("*.part"))


def test_unusable_directory_is_rejected_not_raised():
    state = mock_state(lambda _: httpx.Response(200), [])
    result = state.execute_request("download", identifiers=["10.1234/a"], directory="C:/tmp\u0000evil")
    assert not result["accepted"] and result["action"] == "download"
    assert result["error"]["code"] == ErrorCode.INVALID_REQUEST
    assert "usable local path" in result["error"]["message"]


def test_router_normalizes_unexpected_provider_exception():
    class Exploding(FixedProvider):
        def find_pdf(self, record):
            raise RuntimeError("boom for https://api.example.org/x?api_key=SECRETVALUE")
    state = mock_state(lambda _: httpx.Response(404), [Exploding("boom", 10, [], [])])
    state.europepmc.enrich = lambda record: record
    state.pmc_converter.enrich = lambda record: record
    entry = next(e for e in state.resolve("10.1234/a")["errors"] if e["provider"] == "boom")
    assert entry["code"] == "PROVIDER_UNAVAILABLE"
    assert "http" not in entry["message"] and "SECRETVALUE" not in entry["message"]


def test_batch_prefetch_miss_still_resolves_pmcid(tmp_path):
    # The id-converter covers only DOIs Europe PMC cannot map, so an empty prefetch entry
    # must not stop Europe PMC from running as the primary mapper.
    seen = []
    class Recorder(FixedProvider):
        def find_pdf(self, record):
            seen.append((record.doi, record.pmcid))
            return []
    state = mock_state(lambda _: httpx.Response(404), [Recorder("rec", 5, [], [])])
    state.europepmc.enrich = lambda record: setattr(record, "pmcid", "PMC1") or record
    state.pmc_converter.lookup_many = lambda dois: {doi: None for doi in dois}
    state.pmc_converter.enrich = lambda record: record
    state.download_many(["10.1234/a"], str(tmp_path), 1)
    assert ("10.1234/a", "PMC1") in seen


def test_request_validator_is_pure_and_keeps_the_arity_rules():
    assert request_problem(PaperRequest(action="search")) == "search requires a nonempty query"
    assert request_problem(PaperRequest(action="resolve",
                                        identifiers=["10.1234/a", "10.1234/b"])) == \
        "resolve requires exactly one identifier"
    assert request_problem(PaperRequest(action="batch",
                                        identifiers=[f"10.1234/a{i}" for i in range(501)],
                                        directory="D:/x")) == "batch requires 1 to 500 identifiers"
    assert request_problem(PaperRequest(action="batch", identifiers=["10.1234/a"],
                                        directory="D:/x", concurrency=17)) == \
        "concurrency must be an integer from 1 to 16"
    assert request_problem(PaperRequest(action="batch", identifiers=["10.1234/a"],
                                        directory="D:/x")) is None
    assert PaperRequest(action="download", query="A Title").title_download is True
