"""Exact identity, access, and PDF fallback checks for new repositories."""

import httpx
import pytest

from paper_fetch_mcp_safe.config import Config
from paper_fetch_mcp_safe.http import HttpClient
from paper_fetch_mcp_safe.identifiers import parse_identifier
from paper_fetch_mcp_safe.models import FetchError, PaperRecord
from paper_fetch_mcp_safe.providers.europepmc import EuropePmcProvider
from paper_fetch_mcp_safe.providers.hal import HalProvider
from paper_fetch_mcp_safe.providers.osf import OsfPreprintsProvider
from paper_fetch_mcp_safe.providers.pmc import PmcCloudProvider, PmcIdConverter
from paper_fetch_mcp_safe.providers.zenodo import ZenodoProvider
from paper_fetch_mcp_safe.providers.core import CoreProvider
from paper_fetch_mcp_safe.ratelimit import RateLimiter
from paper_fetch_mcp_safe.server import ServerState


def http_with(handler):
    rates = {host: 1000 for host in (
        "www.ebi.ac.uk", "europepmc.org", "pmc.ncbi.nlm.nih.gov",
        "pmc-oa-opendata.s3.amazonaws.com", "api.hal.science",
        "zenodo.org", "api.osf.io", "osf.io",
    )}
    config = Config(mailto="test@example.org")
    return HttpClient(config, RateLimiter(rates=rates), httpx.Client(transport=httpx.MockTransport(handler)))


def test_doi_reaches_existing_europepmc_pdf(tmp_path):
    seen = []
    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "www.ebi.ac.uk":
            return httpx.Response(200, json={"resultList": {"result": [{
                "doi": "10.7554/eLife.19274", "pmcid": "PMC5089862",
                "title": "Open article", "isOpenAccess": "Y",
            }]}})
        if req.url.host == "europepmc.org":
            return httpx.Response(200, content=b"%PDF-1.7\narticle")
        raise AssertionError(f"unexpected request: {req.url}")
    http = http_with(handler)
    state = ServerState(http.config, http, [EuropePmcProvider(http)])
    result = state.download("10.7554/eLife.19274", str(tmp_path))
    assert result["ok"] and result["provider"] == "europepmc"
    assert "DOI%3A10.7554%2FeLife.19274" in seen[0]
    assert not any("idconv" in url for url in seen)


def test_europepmc_rejects_near_match_before_creating_pdf_link():
    data = {"resultList": {"result": [
        {"pmcid": "PMC999", "doi": "10.1234/other", "isOpenAccess": "Y"},
        {"pmcid": "PMC123", "doi": "10.1234/paper", "isOpenAccess": "Y"},
    ]}}
    provider = EuropePmcProvider(http_with(lambda _: httpx.Response(200, json=data)))
    record = PaperRecord(doi="10.1234/paper")
    provider.enrich(record)
    assert record.pmcid == "PMC123"
    assert provider.find_pdf(record)[0].url.endswith("pmcid=PMC123")


def test_pmc_id_converter_fallback_after_empty_europepmc():
    seen = []
    def handler(req):
        seen.append(req)
        if req.url.host == "www.ebi.ac.uk":
            return httpx.Response(200, json={"resultList": {"result": []}})
        return httpx.Response(200, json={"records": [{
            "requested-id": "10.1234/paper", "pmcid": "PMC12345", "pmid": 12345,
        }]})
    http = http_with(handler)
    state = ServerState(http.config, http, [])
    state.crossref.enrich = lambda record: record
    record, errors = state._enrich(PaperRecord(doi="10.1234/paper"))
    assert not errors and record.pmcid == "PMC12345" and record.pmid == "12345"
    assert seen[1].url.params["tool"] == "paper-fetch-mcp-safe"
    assert seen[1].url.params["email"] == "test@example.org"


def test_pmc_converter_batches_at_200_and_a_mapping_suppresses_later_queries():
    seen = []
    def handler(req):
        seen.append(req)
        ids = req.url.params["ids"].split(",")
        return httpx.Response(200, json={"records": [{
            "requested-id": doi, "pmcid": f"PMC{i + 1}", "pmid": i + 1,
        } for i, doi in enumerate(ids) if doi.endswith("paper0")]})
    http = http_with(handler)
    converter = PmcIdConverter(http)
    dois = [f"10.1234/paper{i}" for i in range(201)]
    mapped = converter.lookup_many(dois)
    assert len(seen) == 2 and len(seen[0].url.params["ids"].split(",")) == 200
    assert mapped["10.1234/paper0"]["pmcid"] == "PMC1"
    assert mapped["10.1234/paper200"] is None
    state = ServerState(http.config, http, [])
    state.crossref.enrich = lambda record: record
    before = len(seen)
    record, errors = state._enrich(PaperRecord(doi="10.1234/paper0"), {"pmc_id_converter": mapped})
    assert record.pmcid == "PMC1" and not errors and len(seen) == before


def test_hal_requires_exact_doi_and_uses_main_file():
    def handler(req):
        assert req.url.params["fq"] == "submitType_s:file"
        return httpx.Response(200, json={"response": {"docs": [
            {"doiId_s": "10.1234/other", "fileMain_s": "https://hal.science/other/document"},
            {"doiId_s": "10.1234/paper", "halId_s": "hal-12345", "title_s": ["Paper"],
             "fileMain_s": "https://hal.science/hal-12345/document", "fileLicenses_s": ["cc-by"]},
        ]}})
    record = PaperRecord(doi="10.1234/paper")
    links = HalProvider(http_with(handler)).find_pdf(record)
    assert len(links) == 1 and links[0].url.endswith("hal-12345/document")
    assert links[0].license == "cc-by" and record.title == "Paper"


def test_zenodo_public_article_pdf_only_and_dataset_rejected():
    article = {"id": 123, "doi": "10.5281/zenodo.123", "metadata": {
        "title": "A paper", "upload_type": "publication", "publication_type": "preprint",
        "access_right": "open", "license": {"id": "cc-by-4.0"}},
        "files": [
            {"key": "paper.pdf", "links": {"self": "https://zenodo.org/api/files/x/paper.pdf"}},
            {"key": "supplement.pdf", "links": {"self": "https://zenodo.org/api/files/x/supplement.pdf"}},
        ]}
    provider = ZenodoProvider(http_with(lambda _: httpx.Response(200, json=article)))
    links = provider.find_pdf(parse_identifier("10.5281/zenodo.123"))
    assert len(links) == 1 and links[0].version == "preprint"
    dataset = {**article, "metadata": {**article["metadata"], "upload_type": "dataset"}}
    assert ZenodoProvider(http_with(lambda _: httpx.Response(200, json=dataset))).find_pdf(
        parse_identifier("zenodo:123")) == []


def test_zenodo_generic_doi_requires_explicit_publication_relation():
    record = {"id": 321, "metadata": {"upload_type": "publication", "access_right": "open",
        "related_identifiers": [{"identifier": "10.1234/paper", "relation": "isPreprintOf"}]},
        "files": [{"key": "article.pdf", "links": {"self": "https://zenodo.org/api/files/x/article.pdf"}}]}
    provider = ZenodoProvider(http_with(lambda _: httpx.Response(200, json={"hits": {"hits": [record]}})))
    assert len(provider.find_pdf(PaperRecord(doi="10.1234/paper"))) == 1
    record["metadata"]["related_identifiers"][0]["relation"] = "isSupplementTo"
    assert provider.find_pdf(PaperRecord(doi="10.1234/paper")) == []


def test_osf_exact_preprint_and_withdrawal_filter():
    data = {"data": {"id": "hqb2p_v1", "attributes": {"public": True,
        "is_published": True, "reviews_state": "accepted", "date_withdrawn": None,
        "title": "Preprint"}, "relationships": {"primary_file": {"data": {"id": "file1"}}}}}
    record = parse_identifier("https://osf.io/preprints/socarxiv/hqb2p_v1/")
    def handler(req):
        if "/files/" in req.url.path:
            return httpx.Response(200, json={"data": {"id": "file1", "attributes": {"name": "Preprint.pdf"},
                "links": {"download": "https://osf.io/download/file1/"}}})
        return httpx.Response(200, json=data)
    provider = OsfPreprintsProvider(http_with(handler))
    links = provider.find_pdf(record)
    assert links[0].url == "https://osf.io/download/file1/"
    assert record.title == "Preprint"
    data["data"]["attributes"]["date_withdrawn"] = "2025-01-01"
    assert provider.find_pdf(parse_identifier("osf:hqb2p_v1")) == []


def test_pmc_cloud_only_returns_oa_pdf_and_falls_back_from_broken_europepmc(tmp_path):
    listing = b'''<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <CommonPrefixes><Prefix>PMC12345.1/</Prefix></CommonPrefixes></ListBucketResult>'''
    def handler(req):
        if req.url.host == "www.ebi.ac.uk":
            return httpx.Response(200, json={"resultList": {"result": [{
                "pmcid": "PMC12345", "isOpenAccess": "Y", "title": "Article"}]}})
        if req.url.host == "europepmc.org":
            return httpx.Response(200, content=b"HTML challenge")
        if req.url.host == "pmc-oa-opendata.s3.amazonaws.com":
            if req.url.path == "/":
                return httpx.Response(200, content=listing)
            if req.url.path.endswith(".json"):
                return httpx.Response(200, json={"pmcid": "PMC12345", "is_pmc_openaccess": "yes",
                    "is_retracted": "no", "license_code": "CC BY", "pdf_url":
                    "s3://pmc-oa-opendata/PMC12345.1/PMC12345.1.pdf"})
            return httpx.Response(200, content=b"%PDF-1.7\narchive")
        raise AssertionError(req.url)
    http = http_with(handler)
    state = ServerState(http.config, http, [EuropePmcProvider(http), PmcCloudProvider(http)])
    result = state.download("PMC12345", str(tmp_path))
    assert result["ok"] and result["provider"] == "pmc_cloud"


def test_pmc_cloud_marks_no_dataset_pdf_for_author_manuscript():
    listing = b"<ListBucketResult><CommonPrefixes><Prefix>PMC12345.1/</Prefix></CommonPrefixes></ListBucketResult>"
    def handler(req):
        if req.url.path == "/":
            return httpx.Response(200, content=listing)
        return httpx.Response(200, json={"pmcid": "PMC12345", "is_pmc_openaccess": "no",
            "is_manuscript": "yes", "license_code": "TDM", "pdf_url": None})
    record = PaperRecord(pmcid="PMC12345")
    assert PmcCloudProvider(http_with(handler)).find_pdf(record) == []
    assert record.extra["pmc_dataset_pdf"] == "not_available"


def test_pmc_author_manuscript_failure_explains_pdf_limit(tmp_path):
    def handler(req):
        if req.url.host == "www.ebi.ac.uk":
            return httpx.Response(200, json={"resultList": {"result": [{
                "pmcid": "PMC3744390", "isOpenAccess": "N", "title": "Author manuscript"}]}})
        if req.url.host == "pmc-oa-opendata.s3.amazonaws.com":
            return httpx.Response(200, content=b"<ListBucketResult/>")
        raise AssertionError(req.url)
    http = http_with(handler)
    state = ServerState(http.config, http, [EuropePmcProvider(http), PmcCloudProvider(http)])
    result = state.download("PMC3744390", str(tmp_path))
    assert not result["ok"] and "No PDF found in PMC Article Datasets" in result["warning"]


def test_unconfigured_core_is_skipped_without_provider_failure():
    http = http_with(lambda _: (_ for _ in ()).throw(AssertionError("CORE must not be queried")))
    provider = CoreProvider(http)
    assert not provider.supports(PaperRecord(doi="10.1234/a"))
    assert provider.find_pdf(PaperRecord(doi="10.1234/a")) == []


def test_repository_urls_parse_without_accepting_osf_file_url():
    assert parse_identifier("https://zenodo.org/record/1062358").extra["zenodo_id"] == "1062358"
    assert parse_identifier("https://hal.science/hal-00721635v2/document").extra["hal_id"] == "hal-00721635v2"
    assert parse_identifier("https://osf.io/preprints/socarxiv/hqb2p_v1/").extra["osf_id"] == "hqb2p_v1"
    with pytest.raises(FetchError):
        parse_identifier("https://osf.io/download/uksw6/")
