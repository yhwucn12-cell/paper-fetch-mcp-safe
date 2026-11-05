"""Thread-safe host pacing, quota observations, and circuit breakers."""

from __future__ import annotations

import threading
import time
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .models import ErrorCode, FetchError

DEFAULT_RATES: dict[str, float] = {
    "api.openalex.org": 10.0,
    "api.unpaywall.org": 5.0,
    "api.crossref.org": 50.0,
    "api.semanticscholar.org": 1.0,
    "api.core.ac.uk": 0.5,
    "export.arxiv.org": 1 / 3,
    "arxiv.org": 1 / 3,
    "www.ebi.ac.uk": 10.0,          # Europe PMC REST (search / mapping / PDF links)
    "europepmc.org": 1.0,           # Europe PMC bulk PDF host: paced deliberately slower
    "pmc.ncbi.nlm.nih.gov": 3.0,
    "pmc-oa-opendata.s3.amazonaws.com": 2.0,
    "api.hal.science": 2.0,
    "zenodo.org": 0.5,
    "api.osf.io": 1.0,
    "osf.io": 1.0,
    "www.biorxiv.org": 1.0,
    "www.medrxiv.org": 1.0,
    "openreview.net": 1.0,
    "api2.openreview.net": 1.0,
    "api.biorxiv.org": 1.0,
    "aclanthology.org": 1.0,
    "proceedings.mlr.press": 1.0,
}


@dataclass(slots=True)
class HostState:
    rate: float
    lock: threading.RLock = field(default_factory=threading.RLock)
    last_start: float = 0.0
    open_until: float = 0.0
    failures: int = 0
    quota_remaining: float | None = None
    quota_reset_seconds: float | None = None
    daily_count: int = 0
    daily_day: int = 0
    openalex_daily_spend: float = 0.0
    openalex_daily_budget: float | None = None


class RateLimiter:
    def __init__(self, rates: dict[str, float] | None = None, clock=time.monotonic, sleeper=time.sleep):
        self.rates = dict(DEFAULT_RATES if rates is None else rates)
        self._states: dict[str, HostState] = {}
        self._guard = threading.Lock()
        self._clock = clock
        self._sleep = sleeper

    def _state(self, host: str) -> HostState:
        with self._guard:
            if host not in self._states:
                self._states[host] = HostState(rate=self.rates.get(host, 1.0))
            return self._states[host]

    def set_rate(self, host: str, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rates[host] = rate
        state = self._state(host)
        with state.lock:
            state.rate = rate

    def reserve_openalex(self, cost_usd: float, budget_usd: float) -> None:
        """Conservatively track documented free-tier query costs per UTC day."""
        state = self._state("api.openalex.org")
        with state.lock:
            day = int(time.time() // 86400)
            if state.daily_day != day:
                state.daily_day = day
                state.openalex_daily_spend = 0.0
            state.openalex_daily_budget = budget_usd
            if state.openalex_daily_spend + cost_usd > budget_usd + 1e-9:
                raise FetchError(ErrorCode.RATE_LIMITED, "OpenAlex local daily budget reached",
                                 86400 - time.time() % 86400)
            state.openalex_daily_spend += cost_usd

    @contextmanager
    def slot(self, host: str) -> Iterator[None]:
        """Hold one host's lock through the entire HTTP response body."""
        state = self._state(host)
        with state.lock:
            now = self._clock()
            if state.open_until > now:
                raise FetchError(ErrorCode.RATE_LIMITED, f"{host} is cooling down", state.open_until - now)
            if host == "api.unpaywall.org":
                day = int(time.time() // 86400)
                if state.daily_day != day:
                    state.daily_day, state.daily_count = day, 0
                if state.daily_count >= 100_000:
                    raise FetchError(ErrorCode.RATE_LIMITED, "Unpaywall daily quota reached", 86400 - time.time() % 86400)
                state.daily_count += 1
            delay = max(0.0, state.last_start + 1 / state.rate - now) if state.last_start else 0.0
            if delay:
                self._sleep(delay)
            state.last_start = self._clock()
            yield

    def observe(self, host: str, headers: dict | object) -> None:
        state = self._state(host)
        with state.lock:
            get = getattr(headers, "get")
            for key in ("x-ratelimit-remaining", "x-rate-limit-remaining", "cr-tdm-ratelimit-remaining"):
                if (raw := get(key)) is not None:
                    try:
                        state.quota_remaining = float(raw)
                    except (TypeError, ValueError):
                        pass
                    break
            raw = next((get(key) for key in ("x-ratelimit-reset", "x-rate-limit-reset", "cr-tdm-ratelimit-reset")
                        if get(key) is not None), None)
            if raw is not None:
                try:
                    value = str(raw).strip().lower()
                    if match := re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", value):
                        reset = float(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
                    else:
                        reset = float(value)
                        if reset > time.time() - 86400:
                            reset -= time.time()
                    state.quota_reset_seconds = max(0.0, reset)
                except (TypeError, ValueError):
                    pass
            if state.quota_remaining is not None and state.quota_remaining <= 0 and state.quota_reset_seconds:
                state.open_until = max(state.open_until, self._clock() + min(900.0, state.quota_reset_seconds))

    def record_success(self, host: str) -> None:
        state = self._state(host)
        with state.lock:
            state.failures = 0

    def record_failure(self, host: str, status: int, retry_after: float | None = None) -> float:
        state = self._state(host)
        with state.lock:
            if status >= 500:
                state.failures += 1
            backoff = min(900.0, max(0.0, retry_after if retry_after is not None else 2 ** max(1, state.failures)))
            if status == 429 or status == 503:
                state.open_until = max(state.open_until, self._clock() + backoff)
            if status >= 500 and state.failures >= 3:
                state.open_until = max(state.open_until, self._clock() + 900.0)
            return backoff

    def snapshot(self) -> dict[str, dict]:
        for host in self.rates:
            self._state(host)
        with self._guard:
            items = list(self._states.items())
        out: dict[str, dict] = {}
        for host, state in items:
            with state.lock:
                now = self._clock()
                out[host] = {
                    "requests_per_second": state.rate,
                    "next_request_in": max(0.0, state.last_start + 1 / state.rate - now) if state.last_start else 0.0,
                    "retry_after": max(0.0, state.open_until - now),
                    "quota_remaining": state.quota_remaining,
                    "quota_reset_seconds": state.quota_reset_seconds,
                    "unpaywall_daily_remaining": max(0, 100_000 - state.daily_count) if host == "api.unpaywall.org" else None,
                    "openalex_daily_remaining_usd": round(max(0.0, state.openalex_daily_budget - state.openalex_daily_spend), 6)
                    if host == "api.openalex.org" and state.openalex_daily_budget is not None else None,
                }
        return out
