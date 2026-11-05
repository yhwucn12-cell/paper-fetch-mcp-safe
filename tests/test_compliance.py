"""The copyright-safety boundary: licence parsing, policy decisions, and enforcement.

These tests are the contract for the fork's central change. They are deliberately
offline — the policy is pure, so every branch can be exercised without a network.
"""

import json

import httpx
import pytest

from paper_fetch_mcp_safe import compliance
from paper_fetch_mcp_safe.compliance import evaluate, enforce, normalize_license
from paper_fetch_mcp_safe.config import Config
from paper_fetch_mcp_safe.http import HttpClient
from paper_fetch_mcp_safe.models import ErrorCode, FetchError, PaperRecord, PdfCandidate
from paper_fetch_mcp_safe.ratelimit import RateLimiter
from paper_fetch_mcp_safe.router import build_registry
from paper_fetch_mcp_safe.server import ServerState


@pytest.mark.parametrize("raw,expected", [
    ("CC BY 4.0", "cc-by"),
    ("cc_by", "cc-by"),
    ("cc-by", "cc-by"),
    ("https://creativecommons.org/licenses/by/4.0/", "cc-by"),
    ("CC-BY-NC-SA 4.0", "cc-by-nc-sa"),
    ("CC BY-ND", "cc-by-nd"),
    ("CC0 1.0", "cc0"),
    ("cc-0", "cc0"),
    ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
    ("public-domain", "cc0"),
    (None, None),
    ("", None),
    ("all rights reserved", None),
    ("elsevier-user-license", None),
    ("TDM", None),
])
def test_licence_spellings_normalize_without_guessing(raw, expected):
    assert normalize_license(raw) == expected


@pytest.mark.parametrize("normalized,klass", [
    ("cc-by", "open"),
    ("cc-by-sa", "open-sharealike"),
    ("cc-by-nc", "open-nc"),
    ("cc-by-nd", "open-nd"),
    ("cc-by-nc-nd", "open-nc-nd"),
    ("cc0", "public-domain"),
    (None, "unknown"),
])
def test_licence_classes(normalized, klass):
    assert compliance.license_class(normalized) == klass


def test_unknown_licence_from_aggregator_is_refused():
    decision = evaluate(PdfCandidate(url="https://x.example/a.pdf", provider="openalex"))
    assert not decision.allowed
    assert decision.evidence == "none"
    assert "open-licence evidence" in decision.reason


def test_explicit_open_licence_is_accepted_from_any_provider():
    decision = evaluate(PdfCandidate(url="https://x.example/a.pdf", provider="openalex", license="cc-by"))
    assert decision.allowed and decision.evidence == "license" and decision.license_class == "open"


def test_restrictive_creative_commons_is_accepted_but_warned():
    decision = evaluate(PdfCandidate(url="https://x.example/a.pdf", provider="openalex",
                                     license="CC BY-NC-ND 4.0"))
    assert decision.allowed
    joined = " ".join(decision.warnings)
    assert "Non-commercial" in joined and "No-derivatives" in joined


def test_source_attested_provider_needs_no_licence_field():
    decision = evaluate(PdfCandidate(url="https://arxiv.org/pdf/1706.03762", provider="arxiv"))
    assert decision.allowed and decision.evidence == "source-attested"


def test_oa_declared_status_is_weaker_evidence_than_a_licence():
    decision = evaluate(PdfCandidate(url="https://x.example/a.pdf", provider="unpaywall", oa_status="gold"))
    assert decision.allowed and decision.evidence == "oa-declared"
    assert compliance.OA_DECLARED_WARNING in decision.warnings
    bronze = evaluate(PdfCandidate(url="https://x.example/a.pdf", provider="openalex", oa_status="bronze"))
    assert bronze.allowed and any("Bronze" in warning for warning in bronze.warnings)


def test_strict_policy_requires_a_real_licence_or_attesting_source():
    aggregator = PdfCandidate(url="https://x.example/a.pdf", provider="openalex", oa_status="gold")
    assert evaluate(aggregator, license_policy="strict").allowed is False
    assert evaluate(PdfCandidate(url="https://arxiv.org/pdf/1", provider="arxiv"),
                    license_policy="strict").allowed is True
    assert evaluate(PdfCandidate(url="https://x.example/a.pdf", provider="openalex", license="cc-by"),
                    license_policy="strict").allowed is True


def test_gray_is_refused_by_default_and_flagged_when_enabled():
    gray = PdfCandidate(url="https://sci-hub.example/a.pdf", provider="scihub", compliance_tier="gray")
    refused = evaluate(gray)
    assert not refused.allowed and "allow_scihub = false" in refused.reason
    allowed = evaluate(gray, allow_gray=True)
    assert allowed.allowed and allowed.tier == "gray"
    assert compliance.GRAY_WARNING in allowed.warnings


def test_strict_policy_refuses_gray_even_when_the_source_is_enabled():
    gray = PdfCandidate(url="https://sci-hub.example/a.pdf", provider="scihub", compliance_tier="gray")
    assert evaluate(gray, allow_gray=True, license_policy="strict").allowed is False
    assert evaluate(gray, allow_gray=True, license_policy="any").allowed is True


def test_unknown_tier_is_never_treated_as_open_access():
    decision = evaluate(PdfCandidate(url="https://x.example/a.pdf", provider="arxiv",
                                     compliance_tier="mystery"))
    assert not decision.allowed and decision.evidence == "none"


def test_any_policy_allows_unlicensed_but_says_so():
    decision = evaluate(PdfCandidate(url="https://x.example/a.pdf", provider="openalex"),
                        license_policy="any")
    assert decision.allowed and decision.evidence == "none"
    assert compliance.UNVERIFIED_WARNING in decision.warnings


def test_enforce_raises_the_documented_error_code():
    with pytest.raises(FetchError) as exc:
        enforce(PdfCandidate(url="https://x.example/a.pdf", provider="openalex"))
    assert exc.value.code == ErrorCode.LICENSE_RESTRICTED


def test_decision_serializes_for_tool_output():
    payload = evaluate(PdfCandidate(url="https://x.example/a.pdf", provider="arxiv")).to_dict()
    assert payload["allowed"] is True and payload["evidence"] == "source-attested"
    json.dumps(payload)


def test_downloader_refuses_before_any_request(tmp_path):
    calls = []
    http = HttpClient(Config(mailto="test@example.org"),
                      RateLimiter(rates={"example.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(
                          lambda request: calls.append(str(request.url)) or httpx.Response(200))))
    with pytest.raises(FetchError) as exc:
        http.download_pdf(PdfCandidate(url="https://example.org/a.pdf", provider="openalex"),
                          PaperRecord(doi="10.1234/a"), tmp_path / "a.pdf")
    assert exc.value.code == ErrorCode.LICENSE_RESTRICTED
    assert calls == [] and list(tmp_path.iterdir()) == []


def test_downloader_refuses_gray_while_gray_sources_are_disabled(tmp_path):
    http = HttpClient(Config(mailto="test@example.org"),
                      RateLimiter(rates={"example.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(
                          lambda _: httpx.Response(200, content=b"%PDF-1.7"))))
    with pytest.raises(FetchError) as exc:
        http.download_pdf(PdfCandidate(url="https://example.org/a.pdf", provider="scihub",
                                       compliance_tier="gray"),
                          PaperRecord(doi="10.1234/a"), tmp_path / "a.pdf")
    assert exc.value.code == ErrorCode.LICENSE_RESTRICTED
    assert list(tmp_path.iterdir()) == []


def test_sidecar_records_how_the_licence_was_established(tmp_path):
    http = HttpClient(Config(mailto="test@example.org"),
                      RateLimiter(rates={"example.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(
                          lambda _: httpx.Response(200, content=b"%PDF-1.7\nbody"))))
    http.download_pdf(PdfCandidate(url="https://arxiv.org/pdf/1706.03762", provider="arxiv"),
                      PaperRecord(arxiv_id="1706.03762"), tmp_path / "a.pdf")
    sidecar = json.loads((tmp_path / "a.pdf.json").read_text(encoding="utf-8"))
    assert sidecar["license_evidence"] == "source-attested"
    assert sidecar["license_normalized"] is None
    assert sidecar["license_class"] == "unknown"
    assert sidecar["policy_version"] == compliance.POLICY_VERSION


def _state(tmp_path, *, license_policy="open", allow_scihub=False, links=("https://good.example/a.pdf",)):
    class Fixed:
        name = "openalex"
        priority = 10
        compliance_tier = "oa"

        def supports(self, record):
            return True

        def find_pdf(self, record):
            return [PdfCandidate(url=url, provider=self.name) for url in links]

    config = Config(mailto="test@example.org", license_policy=license_policy, allow_scihub=allow_scihub)
    http = HttpClient(config, RateLimiter(rates={"good.example": 100}),
                      httpx.Client(transport=httpx.MockTransport(
                          lambda _: httpx.Response(200, content=b"%PDF-1.7\nok"))))
    state = ServerState(config, http, [Fixed()])
    state.crossref.enrich = lambda record: record
    state.europepmc.enrich = lambda record: record
    state.pmc_converter.enrich = lambda record: record
    return state


def test_unlicensed_candidate_surfaces_as_licence_restricted_not_paywall(tmp_path):
    state = _state(tmp_path)
    result = state.download("10.1234/a", str(tmp_path))
    assert not result["ok"]
    assert result["error"]["code"] == ErrorCode.LICENSE_RESTRICTED
    assert list(tmp_path.iterdir()) == []


def test_provider_failure_outranks_a_refusal_because_the_search_was_incomplete(tmp_path):
    # A refusal plus an unrelated source failure must not read as "this paper is
    # restricted": we did not finish looking, so the honest answer is inconclusive.
    class Failing:
        name = "broken"
        priority = 5
        compliance_tier = "oa"

        def supports(self, record):
            return True

        def find_pdf(self, record):
            raise FetchError(ErrorCode.PROVIDER_UNAVAILABLE, "upstream down")

    state = _state(tmp_path)
    state.registry = [Failing(), *state.registry]
    result = state.download("10.1234/a", str(tmp_path))
    assert result["error"]["code"] == ErrorCode.PROVIDER_UNAVAILABLE


def test_operator_can_relax_the_policy_but_the_result_is_marked(tmp_path):
    state = _state(tmp_path, license_policy="any")
    result = state.download("10.1234/a", str(tmp_path))
    assert result["ok"] and result["license_evidence"] == "none"
    assert result["license_class"] == "unknown"
    assert compliance.UNVERIFIED_WARNING in result["warning"]
    assert result["compliance_tier"] == "oa"


def test_resolve_reports_refusals_instead_of_silently_dropping_candidates():
    state = _state(".", )
    resolved = state.resolve("10.1234/a")
    assert resolved["candidates"] == []
    codes = {error["code"] for error in resolved["errors"]}
    assert ErrorCode.LICENSE_RESTRICTED.value in codes
    assert resolved["error"]["code"] == ErrorCode.LICENSE_RESTRICTED


def test_default_registry_excludes_gray_and_status_publishes_the_policy():
    http = HttpClient(Config(mailto="test@example.org"),
                      RateLimiter(rates={"api.openalex.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))))
    assert "scihub" not in [provider.name for provider in build_registry(http)]
    state = ServerState(http.config, http, build_registry(http))
    status = state.status()
    assert status["scihub_enabled"] is False
    assert status["compliance"]["license_policy"] == "open"
    assert status["compliance"]["policy_version"] == compliance.POLICY_VERSION
    assert status["compliance"]["license_required_by_default"] is True
    assert "openalex" not in status["compliance"]["source_attested_providers"]


def test_search_does_not_advertise_unlicensed_links():
    body = {"results": [
        {"title": "No licence", "doi": "https://doi.org/10.1234/a",
         "open_access": {"is_oa": True}, "best_oa_location": {"pdf_url": "https://x.example/a.pdf"}},
        {"title": "Gold OA", "doi": "https://doi.org/10.1234/b", "open_access": {"is_oa": True, "oa_status": "gold"},
         "best_oa_location": {"pdf_url": "https://x.example/b.pdf", "is_oa": True}},
        {"title": "Licensed", "doi": "https://doi.org/10.1234/c", "open_access": {"is_oa": True},
         "best_oa_location": {"pdf_url": "https://x.example/c.pdf", "is_oa": True, "license": "cc-by"}},
    ]}
    state = _state(".", )
    state.http = HttpClient(Config(mailto="test@example.org"),
                            RateLimiter(rates={"api.openalex.org": 100}),
                            httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))))
    papers = state.search("paper", source="openalex")["papers"]
    assert [paper["title"] for paper in papers] == ["No licence", "Gold OA", "Licensed"]
    assert papers[0]["pdf_candidates"] == []
    assert len(papers[1]["pdf_candidates"]) == 1 and len(papers[2]["pdf_candidates"]) == 1


def test_batch_summary_counts_licence_refusals_separately(tmp_path):
    state = _state(tmp_path)
    summary = state.execute_request("batch", identifiers=["10.1234/a"], directory=str(tmp_path))["result"]
    assert summary["failed"] == 1 and summary["license_restricted"] == 1
    assert summary["failures"] == [{"identifier": "doi:10.1234/a", "code": "LICENSE_RESTRICTED"}]
