"""Retry, redirect, quota and file integrity boundary checks."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import httpx
import pytest

from paper_fetch_mcp_safe.config import Config
from paper_fetch_mcp_safe.http import GRAY_USER_AGENT, HttpClient, parse_retry_after
import paper_fetch_mcp_safe.http as http_module
from paper_fetch_mcp_safe.models import ErrorCode, FetchError, PaperRecord, PdfCandidate
from paper_fetch_mcp_safe.ratelimit import RateLimiter


@pytest.mark.parametrize("kind", ["json", "text", "post"])
def test_live_style_streamed_metadata_response(kind):
    class NetworkStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"ok": true}' if kind != "text" else b"<title>Paper</title>"

    http = HttpClient(
        Config(mailto="test@example.org"),
        RateLimiter(rates={"example.org": 100}),
        httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=NetworkStream())
        )),
    )
    if kind == "text":
        assert http.get_text("https://example.org/paper") == "<title>Paper</title>"
    elif kind == "post":
        assert http.post_json("https://example.org/paper", {"query": "paper"}) == {"ok": True}
    else:
        assert http.get_json("https://example.org/paper") == {"ok": True}


def test_loopback_candidate_is_refused_without_a_request(tmp_path):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-x")
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"127.0.0.1": 100}),
                      httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(FetchError) as exc:
        http.download_pdf(PdfCandidate(url="http://127.0.0.1:9/internal.pdf", provider="test", license="cc-by"),
                          PaperRecord(doi="10.1234/a"), tmp_path / "a.pdf")
    assert exc.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert calls == [] and list(tmp_path.iterdir()) == []


def test_oversized_stream_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(http_module, "MAX_DOWNLOAD_BYTES", 64)
    class Big(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(8):
                yield b"%PDF-" + b"x" * 1020
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"example.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Big()))))
    with pytest.raises(FetchError) as exc:
        http.download_pdf(PdfCandidate(url="https://example.org/a", provider="test", license="cc-by"),
                          PaperRecord(doi="10.1234/a"), tmp_path / "a.pdf")
    assert exc.value.code == ErrorCode.DOWNLOAD_CORRUPT
    assert list(tmp_path.iterdir()) == []


def test_declared_oversize_is_refused_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(http_module, "MAX_DOWNLOAD_BYTES", 64)
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"example.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(
                          lambda _: httpx.Response(200, content=b"%PDF-1.7" + b"x" * 4096))))
    with pytest.raises(FetchError) as exc:
        http.download_pdf(PdfCandidate(url="https://example.org/a", provider="test", license="cc-by"),
                          PaperRecord(doi="10.1234/a"), tmp_path / "a.pdf")
    assert exc.value.code == ErrorCode.DOWNLOAD_CORRUPT
    assert list(tmp_path.iterdir()) == []

def test_gray_candidate_uses_browser_user_agent(tmp_path):
    seen = {}
    def handler(request):
        seen["user-agent"] = request.headers["user-agent"]
        return httpx.Response(200, content=b"%PDF-gray")
    http = HttpClient(Config(mailto="test@example.org", allow_scihub=True),
                      RateLimiter(rates={"example.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(handler)))
    http.download_pdf(PdfCandidate(url="https://example.org/a", provider="scihub", compliance_tier="gray"),
                      PaperRecord(doi="10.1234/a"), tmp_path / "gray.pdf")
    assert seen["user-agent"] == GRAY_USER_AGENT


def test_oa_candidate_keeps_polite_user_agent(tmp_path):
    seen = {}
    def handler(request):
        seen["user-agent"] = request.headers["user-agent"]
        return httpx.Response(200, content=b"%PDF-oa")
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"example.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(handler)))
    http.download_pdf(PdfCandidate(url="https://example.org/a", provider="openalex", license="cc-by"),
                      PaperRecord(doi="10.1234/a"), tmp_path / "oa.pdf")
    assert seen["user-agent"] == "paper-fetch-mcp-safe/0.2 (mailto:test@example.org)"


def test_retry_after_http_date_and_quota_reset():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10))
    assert 8 <= parse_retry_after(future) <= 11
    limiter = RateLimiter(rates={"example.org": 10})
    limiter.observe("example.org", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "15s"})
    state = limiter.snapshot()["example.org"]
    assert state["quota_remaining"] == 0
    assert state["retry_after"] > 0
    limiter.observe("crossref.org", {"cr-tdm-ratelimit-remaining": "0", "cr-tdm-ratelimit-reset": "1m"})
    assert limiter.snapshot()["crossref.org"]["retry_after"] > 0


def test_429_retries_then_succeeds(monkeypatch):
    now = [100.0]
    limiter = RateLimiter(rates={"example.org": 100}, clock=lambda: now[0],
                          sleeper=lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr("paper_fetch_mcp_safe.http.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    attempts = []
    def handler(req):
        attempts.append(req)
        return httpx.Response(429, headers={"retry-after": "2"}) if len(attempts) == 1 else httpx.Response(200, json={"ok": True})
    http = HttpClient(Config(mailto="test@example.org"), limiter, httpx.Client(transport=httpx.MockTransport(handler)))
    assert http.get_json("https://example.org/test") == {"ok": True}
    assert len(attempts) == 2


def test_transient_transport_failure_retries_metadata_once(monkeypatch):
    monkeypatch.setattr("paper_fetch_mcp_safe.http.time.sleep", lambda _: None)
    calls = [0]
    def handler(req):
        calls[0] += 1
        if calls[0] == 1:
            raise httpx.ConnectError("temporary TLS failure")
        return httpx.Response(200, json={"ok": True})
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"example.org": 1000}),
                      httpx.Client(transport=httpx.MockTransport(handler)))
    assert http.get_json("https://example.org/test") == {"ok": True}
    assert calls[0] == 2


def test_interrupted_pdf_stream_restarts_without_stale_bytes(tmp_path):
    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF-stale"
            raise httpx.ReadError("connection lost")
    calls = [0]
    def handler(req):
        calls[0] += 1
        return httpx.Response(200, stream=Interrupted()) if calls[0] == 1 else httpx.Response(200, content=b"%PDF-good")
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"example.org": 1000}),
                      httpx.Client(transport=httpx.MockTransport(handler)))
    destination = tmp_path / "paper.pdf"
    http.download_pdf(PdfCandidate(url="https://example.org/a", provider="test", license="cc-by"),
                      PaperRecord(doi="10.1234/a"), destination)
    assert calls[0] == 2 and destination.read_bytes() == b"%PDF-good"


def test_three_server_failures_open_circuit(monkeypatch):
    now = [100.0]
    limiter = RateLimiter(rates={"example.org": 100}, clock=lambda: now[0],
                          sleeper=lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr("paper_fetch_mcp_safe.http.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(500)
    http = HttpClient(Config(mailto="test@example.org"), limiter, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(FetchError) as exc:
        http.get_json("https://example.org/test")
    assert exc.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert len(calls) == 3
    assert limiter.snapshot()["example.org"]["retry_after"] >= 899


def test_corrupt_redirect_does_not_replace_existing_pdf(tmp_path):
    destination = tmp_path / "paper.pdf"
    destination.write_bytes(b"%PDF-original")
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"example.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"bad"))))
    with pytest.raises(FileExistsError):
        http.download_pdf(PdfCandidate(url="https://example.org/a", provider="test", license="cc-by"), PaperRecord(doi="10.1234/a"), destination)
    assert destination.read_bytes() == b"%PDF-original"


def test_sidecar_write_failure_rolls_back_pdf(tmp_path, monkeypatch):
    import os
    real_replace = os.replace
    calls = [0]
    def replace(source, destination):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("simulated sidecar failure")
        return real_replace(source, destination)
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"example.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"%PDF-valid"))))
    with monkeypatch.context() as patch:
        patch.setattr("paper_fetch_mcp_safe.http.os.replace", replace)
        with pytest.raises(OSError):
            http.download_pdf(PdfCandidate(url="https://example.org/a", provider="test", license="cc-by"),
                              PaperRecord(doi="10.1234/a"), tmp_path / "paper.pdf")
    assert list(tmp_path.iterdir()) == []


def test_cross_host_redirect_strips_api_credentials():
    seen = []
    def handler(req):
        seen.append(req)
        if req.url.host == "api.example.org":
            return httpx.Response(302, headers={"location": "https://cdn.example.org/data"})
        return httpx.Response(200, json={"ok": True})
    http = HttpClient(Config(mailto="test@example.org"),
                      RateLimiter(rates={"api.example.org": 100, "cdn.example.org": 100}),
                      httpx.Client(transport=httpx.MockTransport(handler)))
    assert http.get_json("https://api.example.org/lookup", params={"api_key": "secret"},
                         headers={"Authorization": "Bearer secret", "x-api-key": "secret"}) == {"ok": True}
    assert "api_key" not in seen[1].url.params
    assert "Authorization" not in seen[1].headers
    assert "x-api-key" not in seen[1].headers


def test_same_host_requests_are_serial_across_threads():
    guard = threading.Lock()
    active = [0]
    peak = [0]
    def handler(req):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.03)
        with guard:
            active[0] -= 1
        return httpx.Response(200, json={"ok": True})
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"example.org": 1000}),
                      httpx.Client(transport=httpx.MockTransport(handler)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: http.get_json("https://example.org/paper"), range(2)))
    assert results == [{"ok": True}, {"ok": True}]
    assert peak[0] == 1


def test_openalex_free_budget_tracks_query_costs():
    http = HttpClient(Config(mailto="test@example.org"), RateLimiter(rates={"api.openalex.org": 1000}),
                      httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []}))))
    http.get_json("https://api.openalex.org/works/https://doi.org/10.1234/a")
    assert http.limiter.snapshot()["api.openalex.org"]["openalex_daily_remaining_usd"] == 0.10
    http.get_json("https://api.openalex.org/works", params={"search": "paper"})
    assert http.limiter.snapshot()["api.openalex.org"]["openalex_daily_remaining_usd"] == 0.099
    for _ in range(99):
        http.limiter.reserve_openalex(0.001, 0.10)
    with pytest.raises(FetchError) as exc:
        http.limiter.reserve_openalex(0.001, 0.10)
    assert exc.value.code == ErrorCode.RATE_LIMITED
