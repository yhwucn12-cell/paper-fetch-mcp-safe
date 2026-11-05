import hashlib
import json
import logging

import httpx
import pytest

from paper_fetch_mcp_safe.config import Config, ConfigError
from paper_fetch_mcp_safe.http import HttpClient, parse_retry_after
from paper_fetch_mcp_safe.identifiers import parse_identifier
from paper_fetch_mcp_safe.models import ErrorCode, FetchError, PaperRecord, PdfCandidate
from paper_fetch_mcp_safe.ratelimit import DEFAULT_RATES, RateLimiter


def test_identifiers_and_config(tmp_path, monkeypatch):
    assert parse_identifier("https://doi.org/10.1371/journal.pone.001").doi == "10.1371/journal.pone.001"
    assert parse_identifier("PMC123").pmcid == "PMC123"
    assert parse_identifier("1706.03762").arxiv_id == "1706.03762"
    assert parse_identifier("https://openreview.net/pdf?id=abc").extra["openreview_id"] == "abc"
    with pytest.raises(FetchError) as exc:
        parse_identifier("garbage")
    assert exc.value.code == ErrorCode.BAD_IDENTIFIER
    with pytest.raises(FetchError):
        parse_identifier("https://fakearxiv.org/abs/1706.03762")
    with pytest.raises(FetchError) as exc:
        parse_identifier("pmlr:not-a-volume")
    assert exc.value.code == ErrorCode.BAD_IDENTIFIER
    config_file = tmp_path / "paper-fetch.toml"
    with pytest.raises(ConfigError):
        Config.from_file(config_file)
    config_file.write_text('mailto = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="mailto"):
        Config.from_file(config_file)
    config_file.write_text('mailto = "test@example.org"\nallow_scihub = false\n', encoding="utf-8")
    monkeypatch.setenv("PAPER_FETCH_MAILTO", "ignored@example.org")
    assert Config.from_file(config_file).mailto == "test@example.org"
    assert not Config.from_file(config_file).allow_scihub
    config_file.write_text('mailto = "test@example.org"\n', encoding="utf-8")
    assert not Config.from_file(config_file).allow_scihub
    assert Config.from_file(config_file).license_policy == "open"
    config_file.write_text('mailto = "test@example.org"\nlicense_policy = "strict"\n', encoding="utf-8")
    assert Config.from_file(config_file).license_policy == "strict"
    config_file.write_text('mailto = "test@example.org"\nlicense_policy = "loose"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="license_policy"):
        Config.from_file(config_file)
    config_file.write_text('mailto = "test@example.org"\nallow_scihub = true\n[host_rates]\n"api.openalex.org" = 2.5\n', encoding="utf-8")
    loaded = Config.from_file(config_file)
    assert loaded.allow_scihub and loaded.host_rates["api.openalex.org"] == 2.5
    config_file.write_text('mailto = "test@example.org"\ndownload_directory = "downloads"\n', encoding="utf-8")
    assert Config.from_file(config_file).download_directory == str(tmp_path / "downloads")
    config_file.write_text('mailto = "test@example.org"\nunknown_key = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown configuration keys"):
        Config.from_file(config_file)


def test_pdf_download_writes_verified_pdf_and_sidecar(tmp_path):
    payload = b"%PDF-1.7\n" + b"x" * 100
    seen = []

    def respond(req):
        seen.append(req.url.host)
        if req.url.host == "source.example":
            return httpx.Response(302, headers={"Location": "https://cdn.example/paper.pdf"})
        return httpx.Response(200, content=payload)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"source.example": 100, "cdn.example": 100}), client)
    candidate = PdfCandidate(url="https://source.example/a", provider="test", license="CC BY 4.0")
    destination = tmp_path / "paper.pdf"
    digest = http.download_pdf(candidate, PaperRecord(doi="10.1234/a"), destination)
    assert seen == ["source.example", "cdn.example"]
    assert destination.read_bytes() == payload
    assert digest == hashlib.sha256(payload).hexdigest()
    sidecar = json.loads((tmp_path / "paper.pdf.json").read_text(encoding="utf-8"))
    assert sidecar["provider"] == "test"
    assert sidecar["sha256"] == digest
    assert sidecar["timestamp"].endswith("+00:00")
    assert sidecar["compliance_tier"] == "oa"
    assert sidecar["license_normalized"] == "cc-by"
    assert sidecar["license_class"] == "open"
    assert sidecar["license_evidence"] == "license"
    assert sidecar["license_policy"] == "open"
    assert sidecar["policy_version"] == "1"


def test_corrupt_download_leaves_no_files(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"<html>")))
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"example.org": 100}), client)
    with pytest.raises(FetchError) as exc:
        http.download_pdf(PdfCandidate(url="https://example.org/a", provider="test", license="cc-by"),
                          PaperRecord(doi="10.1234/a"), tmp_path / "paper.pdf")
    assert exc.value.code == ErrorCode.DOWNLOAD_CORRUPT
    assert list(tmp_path.iterdir()) == []


def test_limiter_circuit_and_retry_after():
    now = [100.0]
    limiter = RateLimiter(rates={"x": 100}, clock=lambda: now[0], sleeper=lambda delay: now.__setitem__(0, now[0] + delay))
    for _ in range(3):
        limiter.record_failure("x", 500)
    with pytest.raises(FetchError) as exc:
        with limiter.slot("x"):
            pass
    assert exc.value.code == ErrorCode.RATE_LIMITED
    assert exc.value.retry_after == 900
    assert parse_retry_after("5") == 5


def test_error_dict_is_terse_and_one_line():
    error = FetchError(ErrorCode.PROVIDER_UNAVAILABLE,
                       "Client error '403 Forbidden' for url 'https://cdn.example/a.pdf'\n"
                       "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403")
    payload = error.to_dict()
    assert payload["code"] == ErrorCode.PROVIDER_UNAVAILABLE
    assert payload["message"] == "HTTP 403 Forbidden"
    assert FetchError(ErrorCode.RATE_LIMITED, "x" * 400, retry_after=20).to_dict()["retry_after"] == 20
    assert FetchError(ErrorCode.PROVIDER_UNAVAILABLE, "x" * 400).to_dict()["message"] == "x" * 119 + "…"
    assert "\n" not in FetchError(ErrorCode.PROVIDER_UNAVAILABLE, "a\nb").to_dict()["message"]


def test_error_log_redacts_credentials(caplog):
    secret = "sk-live-SECRETVALUE"
    error = FetchError(ErrorCode.PROVIDER_UNAVAILABLE,
                       "openalex: search failed: Client error '401 Unauthorized' for url "
                       f"'https://api.openalex.org/works?mailto=a%40b.com&search=x&api_key={secret}'")
    with caplog.at_level(logging.WARNING, logger="paper_fetch_mcp_safe"):
        payload = error.to_dict()
    assert secret not in caplog.text
    assert "api_key=REDACTED" in caplog.text
    assert payload["message"] == "openalex: search failed: HTTP 401 Unauthorized"


def test_short_error_needs_no_log(caplog):
    with caplog.at_level(logging.WARNING, logger="paper_fetch_mcp_safe"):
        FetchError(ErrorCode.PAYWALL_NO_OA, "no registered source returned an OA PDF").to_dict()
    assert caplog.text == ""


def test_rate_table_keys_are_bare_hostnames():
    # The limiter keys on urlparse(...).hostname, which drops the port, so any key carrying
    # one can never match a request and silently falls back to the default rate.
    assert [key for key in DEFAULT_RATES if ":" in key] == []
    assert DEFAULT_RATES["www.ebi.ac.uk"] == 10.0        # Europe PMC REST, documented pace
    assert DEFAULT_RATES.get("europepmc.org", 0) > 0     # bulk PDF host must be explicit
    assert all(rate > 0 for rate in DEFAULT_RATES.values())
