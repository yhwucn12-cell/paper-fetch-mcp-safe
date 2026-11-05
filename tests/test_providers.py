"""Representative source records and exact identifier paths."""

import httpx
import pytest

from paper_fetch_mcp_safe.config import Config
from paper_fetch_mcp_safe.http import GRAY_USER_AGENT, HttpClient
from paper_fetch_mcp_safe.identifiers import parse_identifier
from paper_fetch_mcp_safe.models import ErrorCode, FetchError, PaperRecord
from paper_fetch_mcp_safe.providers.arxiv import ArxivProvider
from paper_fetch_mcp_safe.providers.core import CoreProvider
from paper_fetch_mcp_safe.providers.crossref import CrossrefProvider
from paper_fetch_mcp_safe.providers.europepmc import EuropePmcProvider
from paper_fetch_mcp_safe.providers.openalex import OpenAlexProvider
from paper_fetch_mcp_safe.providers.preprints import PreprintProvider
from paper_fetch_mcp_safe.providers.scihub import SciHubProvider
from paper_fetch_mcp_safe.providers.semantic_scholar import SemanticScholarProvider
from paper_fetch_mcp_safe.providers.unpaywall import UnpaywallProvider
from paper_fetch_mcp_safe.ratelimit import RateLimiter


def make_http(handler, *, core_key=None):
    config = Config(mailto="test@example.org", core_api_key=core_key)
    return HttpClient(config, RateLimiter(rates={}), httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("provider,body,expected", [
    (OpenAlexProvider, {"open_access": {"is_oa": True, "oa_status": "gold"},
                       "best_oa_location": {"is_oa": True, "pdf_url": "https://files.example/a.pdf", "license": "cc-by", "version": "publishedVersion"}}, "gold"),
    (UnpaywallProvider, {"is_oa": True, "oa_status": "green",
                        "best_oa_location": {"url_for_pdf": "https://files.example/a.pdf", "version": "acceptedVersion"}}, "green"),
    (SemanticScholarProvider, {"openAccessPdf": {"url": "https://files.example/a.pdf"}}, "oa"),
    (CoreProvider, {"results": [{"doi": "10.1234/a", "downloadUrl": "https://files.example/a.pdf"}]}, "oa"),
])
def test_doi_aggregators(provider, body, expected):
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=body)
    http = make_http(handler, core_key="key")
    candidates = provider(http).find_pdf(PaperRecord(doi="10.1234/a"))
    assert candidates[0].url == "https://files.example/a.pdf"
    assert candidates[0].oa_status == expected
    assert seen[0].headers["User-Agent"] == "paper-fetch-mcp-safe/0.2 (mailto:test@example.org)"


def test_batch_aggregators_group_and_map():
    def handler(req):
        if req.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": [{"doi": "https://doi.org/10.1234/a",
                "open_access": {"is_oa": True}, "best_oa_location": {"pdf_url": "https://files.example/a.pdf"}}]})
        return httpx.Response(200, json=[{"openAccessPdf": {"url": "https://files.example/a.pdf"}}, None])
    http = make_http(handler)
    dois = ["10.1234/a", "10.1234/b"]
    assert len(OpenAlexProvider(http).find_pdf_batch(dois)["10.1234/a"]) == 1
    assert SemanticScholarProvider(http).find_pdf_batch(dois)["10.1234/b"] == []


def test_openalex_batch_is_chunked_at_fifty():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"results": []})
    config = Config(mailto="test@example.org")
    http = HttpClient(config, RateLimiter(rates={"api.openalex.org": 1000}),
                      httpx.Client(transport=httpx.MockTransport(handler)))
    dois = [f"10.1234/paper{i}" for i in range(120)]
    result = OpenAlexProvider(http).find_pdf_batch(dois)
    assert len(calls) == 3 and len(result) == 120
    assert all(len(req.url.params["filter"].split("|")) <= 50 for req in calls)


def test_europepmc_pmid_mapping_and_pdf():
    def handler(req):
        return httpx.Response(200, json={"resultList": {"result": [{
            "pmid": "12345", "pmcid": "PMC12345", "doi": "10.1234/a", "title": "Open paper",
            "pubYear": "2021", "isOpenAccess": "Y", "license": "cc-by"}]}})
    provider = EuropePmcProvider(make_http(handler))
    record = PaperRecord(pmid="12345")
    provider.enrich(record)
    assert record.pmcid == "PMC12345" and record.doi == "10.1234/a"
    candidate = provider.find_pdf(record)[0]
    assert candidate.url == "https://europepmc.org/api/getPdf?pmcid=PMC12345"
    assert candidate.version is None


def test_arxiv_atom_and_search():
    xml = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>
    <id>http://arxiv.org/abs/1706.03762v7</id><title>Attention Is All You Need</title>
    <published>2017-06-12T00:00:00Z</published><arxiv:doi>10.1234/a</arxiv:doi>
    <link href="https://arxiv.org/pdf/1706.03762" type="application/pdf" /></entry></feed>'''
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, content=xml)
    provider = ArxivProvider(make_http(handler))
    record = PaperRecord(arxiv_id="1706.03762")
    assert provider.find_pdf(record)[0].provider == "arxiv"
    assert record.doi == "10.1234/a"
    assert provider.search("attention", 2, 2017, 2017)[0].arxiv_id == "1706.03762"
    assert seen[0].url.params["id_list"] == "1706.03762"


def test_arxiv_search_joins_bare_terms_with_and():
    # arXiv reads space-separated terms as OR, which turns a topic query into unrelated papers.
    xml = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    seen = []
    def handler(req):
        seen.append(req.url.params["search_query"])
        return httpx.Response(200, content=xml)
    provider = ArxivProvider(make_http(handler))
    provider.search("water oxidation catalyst", 5)
    provider.search("all:electron AND all:proton", 5)
    assert seen == ["all:water AND all:oxidation AND all:catalyst", "all:electron AND all:proton"]


@pytest.mark.parametrize("identifier,host,response,expected", [
    ("openreview:abc123", "api2.openreview.net", {"notes": [{"content": {"title": {"value": "Paper"}, "pdf": {"value": "/pdf?id=abc123"}}}]}, "https://openreview.net/pdf?id=abc123"),
    ("doi:10.1101/2020.03.20.000133", "api.biorxiv.org", {"collection": [{"version": "2", "title": "Preprint", "license": "cc_by"}]}, "https://www.biorxiv.org/content/10.1101/2020.03.20.000133v2.full.pdf"),
])
def test_preprint_api_paths(identifier, host, response, expected):
    def handler(req):
        assert req.url.host == host
        return httpx.Response(200, json=response)
    provider = PreprintProvider(make_http(handler))
    assert provider.find_pdf(parse_identifier(identifier))[0].url == expected


def test_medrxiv_doi_falls_through_biorxiv_lookup():
    seen = []
    def handler(req):
        seen.append(str(req.url))
        if "/biorxiv/" in str(req.url):
            return httpx.Response(200, json={"collection": []})
        return httpx.Response(200, json={"collection": [{"version": "3", "title": "Medical preprint"}]})
    record = parse_identifier("10.1101/2020.03.20.000133")
    candidate = PreprintProvider(make_http(handler)).find_pdf(record)[0]
    assert "/medrxiv/" in seen[-1]
    assert candidate.url.startswith("https://www.medrxiv.org/content/")


def test_scihub_html_candidate_and_mirror_failure():
    config = Config(mailto="test@example.org", allow_scihub=True, scihub_mirrors=["https://mirror.example"])
    http = HttpClient(config, RateLimiter(rates={"mirror.example": 100}),
                      httpx.Client(transport=httpx.MockTransport(
                          lambda _: httpx.Response(200, text='<embed src="//cdn.example/paper.pdf">'))))
    provider = SciHubProvider(http)
    candidate = provider.find_pdf(PaperRecord(doi="10.1234/a"))[0]
    assert candidate.url == "https://cdn.example/paper.pdf"
    assert candidate.compliance_tier == "gray"
    assert http.limiter.snapshot()["mirror.example"]["requests_per_second"] == 0.2
    broken = HttpClient(config, RateLimiter(rates={"mirror.example": 100}),
                        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400))))
    with pytest.raises(FetchError):
        SciHubProvider(broken).find_pdf(PaperRecord(doi="10.1234/a"))


def test_scihub_collects_embeds_from_every_reachable_mirror():
    config = Config(mailto="test@example.org", allow_scihub=True,
                    scihub_mirrors=["https://dead.example", "https://alive.example", "https://also.example"])
    pages = {
        "dead.example": httpx.Response(400),
        "alive.example": httpx.Response(200, text='<iframe src="//cdn.example/a.pdf#view=FitH">'),
        "also.example": httpx.Response(200, text='<embed src="https://cdn.example/a.pdf">'
                                                 '<embed src="//cdn.example/b.pdf">'),
    }
    http = HttpClient(config,
                      RateLimiter(rates={host: 100 for host in pages}),
                      httpx.Client(transport=httpx.MockTransport(lambda req: pages[req.url.host])))
    urls = [candidate.url for candidate in SciHubProvider(http).find_pdf(PaperRecord(doi="10.1234/a"))]
    assert urls == ["https://cdn.example/a.pdf", "https://cdn.example/b.pdf"]


@pytest.mark.parametrize("identifier,domain,pdf", [
    ("acl:2023.acl-long.1", "aclanthology.org", "https://aclanthology.org/2023.acl-long.1.pdf"),
    ("pmlr:v235/sel24a", "proceedings.mlr.press", "https://proceedings.mlr.press/v235/sel24a/sel24a.pdf"),
])
def test_conference_paths(identifier, domain, pdf):
    def handler(req):
        assert req.url.host == domain
        return httpx.Response(200, text='<meta name="citation_title" content="Conference Paper">')
    assert PreprintProvider(make_http(handler)).find_pdf(parse_identifier(identifier))[0].url == pdf


def test_conference_page_pdf_link_wins_over_constructed_path():
    html = '<meta name="citation_pdf_url" content="https://proceedings.mlr.press/v235/alternate.pdf">'
    provider = PreprintProvider(make_http(lambda _: httpx.Response(200, text=html)))
    assert provider.find_pdf(parse_identifier("pmlr:v235/sel24a"))[0].url.endswith("/alternate.pdf")


def test_crossref_only_enriches_metadata():
    def handler(req):
        return httpx.Response(200, json={"message": {"title": ["Example"], "published": {"date-parts": [[2020]]},
                                                   "link": [{"URL": "https://publisher.example/paid.pdf"}]}})
    record = CrossrefProvider(make_http(handler)).enrich(PaperRecord(doi="10.1234/a"))
    assert record.title == "Example" and record.year == 2020
    assert record.pdf_candidates == []
    assert record.extra["crossref_status"] == "found"


def test_missing_fields_and_not_found_are_empty():
    http = make_http(lambda _: httpx.Response(404))
    record = PaperRecord(doi="10.1234/a")
    assert OpenAlexProvider(http).find_pdf(record) == []
    assert SemanticScholarProvider(http).find_pdf(record) == []
    assert UnpaywallProvider(http).find_pdf(record) == []
    assert CoreProvider(make_http(lambda _: httpx.Response(200, json={"results": [{}]}), core_key="key")).find_pdf(record) == []
    assert OpenAlexProvider(http).candidates_from_work({"open_access": {"is_oa": True},
        "locations": [{"is_oa": False, "pdf_url": "https://publisher.example/paywalled.pdf"}]}) == []


@pytest.mark.parametrize("provider,record", [
    (OpenAlexProvider, PaperRecord(doi="10.1234/a")),
    (UnpaywallProvider, PaperRecord(doi="10.1234/a")),
    (SemanticScholarProvider, PaperRecord(doi="10.1234/a")),
    (CoreProvider, PaperRecord(doi="10.1234/a")),
    (EuropePmcProvider, PaperRecord(pmcid="PMC12345")),
    (ArxivProvider, PaperRecord(arxiv_id="1706.03762")),
    (PreprintProvider, PaperRecord(extra={"openreview_id": "abc"})),
])
def test_provider_fault_is_not_reported_as_no_oa(provider, record):
    http = make_http(lambda _: httpx.Response(400), core_key="key")
    with pytest.raises(FetchError) as exc:
        provider(http).find_pdf(record)
    assert exc.value.code == ErrorCode.PROVIDER_UNAVAILABLE


def test_scihub_mirror_page_uses_browser_agent_not_mailto():
    seen = []
    def handler(request):
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(200, text='<embed src="https://cdn.example/a.pdf">')
    config = Config(mailto="private.person@university.edu", allow_scihub=True,
                    scihub_mirrors=["https://mirror.example"])
    http = HttpClient(config, RateLimiter(rates={"mirror.example": 100}),
                      httpx.Client(transport=httpx.MockTransport(handler)))
    SciHubProvider(http).find_pdf(PaperRecord(doi="10.1234/a"))
    assert seen == [GRAY_USER_AGENT] and "mailto" not in seen[0]
