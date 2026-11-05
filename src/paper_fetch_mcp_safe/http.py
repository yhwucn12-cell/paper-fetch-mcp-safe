"""Shared HTTP transport and verified, atomic PDF storage."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlparse

import httpx

from . import compliance
from .config import Config
from .models import ErrorCode, FetchError, PaperRecord, PdfCandidate
from .ratelimit import DEFAULT_RATES, RateLimiter


# Gray mirrors and their CDNs reject requests carrying an identifying UA; present a
# browser UA for gray candidates only, leaving the polite UA on every OA host.
GRAY_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024


@lru_cache(maxsize=256)
def allowed_host(host: str) -> bool:
    """False for loopback and link-local targets, whether literal or resolved.

    Candidate URLs come from third-party metadata, so this is the guard that keeps the
    downloader out of the local machine and the cloud metadata service (169.254.169.254).
    Broader private ranges stay allowed on purpose: institutional mirrors, corporate
    proxies and TUN/VPN resolvers legitimately answer on RFC1918 or reserved space, and a
    blanket "not globally routable" test breaks every one of those setups.
    A host that cannot be resolved passes through: DNS failure is not evidence of a local
    target, and the request then fails at the transport layer anyway.
    """
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return True
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if address.is_loopback or address.is_link_local:
            return False
    return True


def candidate_headers(candidate: PdfCandidate) -> dict[str, dict[str, str]]:
    if candidate.compliance_tier != "gray":
        return {}
    return {"headers": {"User-Agent": GRAY_USER_AGENT, "Accept": "application/pdf,*/*"}}


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class HttpClient:
    def __init__(self, config: Config, limiter: RateLimiter | None = None, client: httpx.Client | None = None):
        self.config = config
        self.limiter = limiter or RateLimiter(rates={**DEFAULT_RATES, **config.host_rates})
        self.limiter.reserve_openalex(0.0, 1.0 if config.openalex_api_key else 0.10)
        self._client = client or httpx.Client(
            headers={"User-Agent": f"paper-fetch-mcp-safe/0.2 (mailto:{config.mailto})"},
            timeout=config.timeout_seconds,
            follow_redirects=False,
        )

    @contextmanager
    def response(self, method: str, url: str, **kwargs) -> Iterator[httpx.Response]:
        """Apply rate control to every redirect host, including streamed bodies."""
        headers = dict(kwargs.pop("headers", {}) or {})
        if not any(key.lower() == "user-agent" for key in headers):
            headers["User-Agent"] = f"paper-fetch-mcp-safe/0.2 (mailto:{self.config.mailto})"
        kwargs["headers"] = headers
        current = url
        redirects = 0
        attempts = 0
        transport_attempts = 0
        while True:
            parsed = urlparse(current)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise FetchError(ErrorCode.PROVIDER_UNAVAILABLE, f"invalid HTTP URL: {current}")
            host = parsed.hostname.lower()
            if not allowed_host(host):
                raise FetchError(ErrorCode.PROVIDER_UNAVAILABLE, f"refusing local network host: {host}")
            retry_delay: float | None = None
            redirect_url: str | None = None
            response_started = False
            with self.limiter.slot(host):
                try:
                    if host == "api.openalex.org":
                        params = kwargs.get("params") or {}
                        cost = 0.001 if "search" in params else 0.0001 if parsed.path.rstrip("/") == "/works" else 0.0
                        self.limiter.reserve_openalex(cost, 1.0 if self.config.openalex_api_key else 0.10)
                    with self._client.stream(method, current, follow_redirects=False, **kwargs) as resp:
                        self.limiter.observe(host, resp.headers)
                        if resp.status_code in {301, 302, 303, 307, 308} and resp.headers.get("location"):
                            redirect_url = urljoin(current, resp.headers["location"])
                            self.limiter.record_success(host)
                        elif resp.status_code in {429, 500, 502, 503, 504}:
                            retry_delay = self.limiter.record_failure(
                                host, resp.status_code, parse_retry_after(resp.headers.get("retry-after"))
                            )
                            attempts += 1
                            if attempts >= 3 or retry_delay > 5:
                                code = ErrorCode.RATE_LIMITED if resp.status_code in {429, 503} else ErrorCode.PROVIDER_UNAVAILABLE
                                raise FetchError(code, f"{host} returned HTTP {resp.status_code}", retry_delay if code == ErrorCode.RATE_LIMITED else None)
                        else:
                            resp.raise_for_status()
                            self.limiter.record_success(host)
                            response_started = True
                            yield resp
                            return
                except httpx.HTTPStatusError:
                    # Providers treat 404 as "record not found"; keep the type intact here.
                    raise
                except httpx.HTTPError as exc:
                    if not response_started and isinstance(exc, httpx.TransportError) and transport_attempts < 1:
                        transport_attempts += 1
                        retry_delay = 1.0
                    else:
                        raise FetchError(ErrorCode.PROVIDER_UNAVAILABLE, f"{host}: {exc}") from exc
            if redirect_url is not None:
                redirects += 1
                if redirects > 5:
                    raise FetchError(ErrorCode.PROVIDER_UNAVAILABLE, "too many redirects")
                target_host = urlparse(redirect_url).hostname
                if target_host != host:
                    kwargs["headers"] = {key: value for key, value in kwargs["headers"].items()
                                         if key.lower() not in {"authorization", "x-api-key"}}
                kwargs.pop("params", None)
                if method.upper() == "POST" and resp.status_code == 303:
                    method = "GET"
                    kwargs.pop("json", None)
                current = redirect_url
                continue
            if retry_delay is not None:
                if retry_delay:
                    time.sleep(retry_delay)
                continue

    def get_json(self, url: str, **kwargs):
        with self.response("GET", url, **kwargs) as resp:
            resp.read()
            return resp.json()

    def get_text(self, url: str, **kwargs) -> str:
        with self.response("GET", url, **kwargs) as resp:
            resp.read()
            return resp.text

    def post_json(self, url: str, body: dict, **kwargs):
        with self.response("POST", url, json=body, **kwargs) as resp:
            resp.read()
            return resp.json()

    def download_pdf(self, candidate: PdfCandidate, record: PaperRecord, destination: Path,
                     *, decision: compliance.PolicyDecision | None = None) -> str:
        """Fetch one candidate, refusing it when the licence policy does not permit it.

        The gate sits here as well as in the router so no code path — including a direct
        caller or a future provider — can write bytes without a recorded decision.
        """
        if decision is None:
            decision = compliance.evaluate(candidate, allow_gray=self.config.allow_scihub,
                                           license_policy=self.config.license_policy)
        if not decision.allowed:
            raise FetchError(ErrorCode.LICENSE_RESTRICTED,
                             decision.reason or "candidate refused by licence policy")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"destination already exists: {destination}")
        temporary: Path | None = None
        sidecar_temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".paper-fetch-", suffix=".part", delete=False) as out:
                temporary = Path(out.name)
                for attempt in range(2):
                    out.seek(0)
                    out.truncate(0)
                    digest = hashlib.sha256()
                    prefix = b""
                    written = 0
                    try:
                        with self.response("GET", candidate.url, **candidate_headers(candidate)) as resp:
                            declared = resp.headers.get("content-length", "")
                            if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                                raise FetchError(ErrorCode.DOWNLOAD_CORRUPT,
                                                 f"{candidate.provider} declared {declared} bytes")
                            for chunk in resp.iter_bytes(64 * 1024):
                                if not chunk:
                                    continue
                                written += len(chunk)
                                if written > MAX_DOWNLOAD_BYTES:
                                    raise FetchError(ErrorCode.DOWNLOAD_CORRUPT,
                                                     f"response exceeded {MAX_DOWNLOAD_BYTES} bytes")
                                if len(prefix) < 4:
                                    prefix += chunk[: 4 - len(prefix)]
                                out.write(chunk)
                                digest.update(chunk)
                        if not prefix.startswith(b"%PDF"):
                            raise FetchError(ErrorCode.DOWNLOAD_CORRUPT, f"{candidate.provider} returned non-PDF content")
                        break
                    except FetchError as exc:
                        if attempt == 0 and isinstance(exc.__cause__, httpx.TransportError):
                            continue
                        raise
            sha = digest.hexdigest()
            metadata = {
                "identifier": record.best_identifier(),
                "doi": record.doi,
                "provider": candidate.provider,
                "url": candidate.url,
                "license": candidate.license,
                "license_normalized": decision.license,
                "license_class": decision.license_class,
                "license_evidence": decision.evidence,
                "license_policy": self.config.license_policy,
                "policy_version": compliance.POLICY_VERSION,
                "version": candidate.version,
                "compliance_tier": candidate.compliance_tier,
                "sha256": sha,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            sidecar = destination.with_suffix(destination.suffix + ".json")
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".paper-fetch-", suffix=".json.part", mode="w", encoding="utf-8", delete=False) as out:
                sidecar_temp = Path(out.name)
                json.dump(metadata, out, indent=2, ensure_ascii=False)
            os.replace(temporary, destination)
            temporary = None
            try:
                os.replace(sidecar_temp, sidecar)
                sidecar_temp = None
            except OSError:
                destination.unlink(missing_ok=True)
                raise
            return sha
        finally:
            for path in (temporary, sidecar_temp):
                if path is not None:
                    path.unlink(missing_ok=True)

    def close(self) -> None:
        self._client.close()
