"""Connector base: polite HTTP (retries, backoff, rate-limit awareness).

Connectors only use official public APIs. They must respect rate limits and
never attempt to bypass authentication or anti-bot measures.
"""

import os
import threading
import time
from abc import ABC, abstractmethod

import httpx

from ..normalize import NormalizedProfile


def worker_processes() -> int:
    """How many worker processes share this machine's request budget.

    Request spacing is kept per process, so N workers each honouring dblp's
    two seconds would send N times the rate dblp asked for. Every worker
    started by `--processes N` sets RIP_WORKER_PROCESSES=N and spaces its
    requests N times wider, so the fleet together is exactly as polite as one
    worker. Throughput still scales, because a request's own latency — about
    a second on OpenAlex — dwarfs the gap enforced between requests.
    """
    try:
        return max(1, int(os.environ.get("RIP_WORKER_PROCESSES", "1")))
    except ValueError:
        return 1


class RateLimitedError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        # The real, source-reported backoff duration, when the source gave
        # one (a Retry-After header, or an X-RateLimit-Reset timestamp) —
        # stored as an actual attribute rather than only baked into the
        # message string, so a caller can use it for a precise backoff
        # instead of guessing a fixed duration. None when the source gave
        # no usable duration; callers fall back to their own default.
        self.retry_after = retry_after


class BaseConnector(ABC):
    source: str = "base"
    source_type: str = "generic"
    # polite minimum delay between requests, per connector instance
    min_request_interval: float = 0.5
    max_retries: int = 3
    # How long one request may take. A source whose latency is unpredictable
    # sets this lower than the default: a live search would rather lose that
    # source for this query than have it hold the whole search open. A timeout
    # is not retried — it propagates, and the caller reports the source failed.
    request_timeout: float = 30.0

    def __init__(self) -> None:
        # One pooled, keep-alive client per connector, shared by every thread
        # (httpx.Client is thread-safe): a live search reuses connections
        # instead of paying a TLS handshake per profile. A short connect
        # timeout stops one unreachable host from stalling a whole search.
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.request_timeout, connect=5.0),
            headers=self.default_headers(),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._last_request_at = 0.0
        self._next_slot = 0.0
        self._slot_lock = threading.Lock()

    def _wait_for_slot(self) -> None:
        """Hold this request until min_request_interval after the previous one.

        Thread-safe: concurrent fetches through one connector are spaced out
        rather than fired together. Each caller reserves the next free slot
        under the lock and sleeps outside it, so waiting never blocks others
        from reserving theirs. Before this, every fetch built its own
        connector with its own clock, and five parallel dblp fetches hit a
        source that asks for two seconds between requests all at once.
        """
        if not hasattr(self, "_slot_lock"):          # built with __new__ (tests)
            self._slot_lock, self._next_slot = threading.Lock(), 0.0
        with self._slot_lock:
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + self.min_request_interval * worker_processes()
        if start > now:
            time.sleep(start - now)

    def default_headers(self) -> dict:
        return {"User-Agent": "resource-intelligence-platform/0.1 (research aggregator)"}

    def get_json(self, url: str, params: dict | None = None) -> dict | list:
        return self._request(url, params).json()

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self._request(url, params).text

    def _request(self, url: str, params: dict | None = None) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            self._wait_for_slot()
            resp = self._client.get(url, params=params)
            self._last_request_at = time.monotonic()
            if resp.status_code in (429, 403) and self._is_rate_limited(resp):
                retry_after = self._retry_after_seconds(resp)
                if retry_after is None or retry_after > 300 or attempt == self.max_retries:
                    raise RateLimitedError(
                        f"{self.source} rate limited (HTTP {resp.status_code}); "
                        f"retry after {retry_after or 'unknown'}s",
                        retry_after=retry_after,
                    )
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:
                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"{self.source}: retries exhausted for {url}")

    def _is_rate_limited(self, resp: httpx.Response) -> bool:
        if resp.status_code == 429:
            return True
        return resp.headers.get("x-ratelimit-remaining") == "0"

    def _retry_after_seconds(self, resp: httpx.Response) -> float | None:
        if resp.headers.get("retry-after"):
            try:
                return float(resp.headers["retry-after"])
            except ValueError:
                return None
        reset = resp.headers.get("x-ratelimit-reset")
        if reset:
            try:
                return max(0.0, float(reset) - time.time())
            except ValueError:
                return None
        return None

    @abstractmethod
    def fetch(self, identifier: str) -> NormalizedProfile:
        """Fetch one person's public data and normalize it."""

    def renormalize(self, external_id: str, raw: dict) -> NormalizedProfile:
        """Re-run normalization from a stored raw payload — no network.

        Lets parser improvements be applied to the whole corpus as a cheap
        local rebuild instead of a full re-crawl.
        """
        raise NotImplementedError(f"{self.source} cannot renormalize from raw")
