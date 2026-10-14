"""
utils/http.py — the one way data fetchers make HTTP calls (REL-02).

`GuardedSession` is a requests.Session that:
- always sets a timeout (connect 3 s, read 10 s by default) — requests has no
  default and waits forever otherwise;
- retries idempotent requests a little (urllib3 Retry: connection errors,
  429 and 5xx, with exponential backoff plus jitter, honouring Retry-After);
- goes through the source's circuit breaker (utils/resilience.py): skipped
  while open, and a final network error / 429 / 5xx counts as a failure.
"""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from utils.resilience import CircuitOpen, breaker

DEFAULT_TIMEOUT = (3.0, 10.0)  # (connect, read) seconds
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0"}


def _retry() -> Retry:
    return Retry(
        total=2,
        connect=2,
        read=1,
        status=2,
        backoff_factor=0.3,
        backoff_jitter=0.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )


class GuardedSession(requests.Session):
    def __init__(self, source: str, default_timeout: Any = DEFAULT_TIMEOUT, headers: dict | None = None):
        super().__init__()
        self.source = source
        self.default_timeout = default_timeout
        self.headers.update(headers or BROWSER_HEADERS)
        adapter = HTTPAdapter(max_retries=_retry())
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def request(self, method, url, *args, **kwargs):
        kwargs.setdefault("timeout", self.default_timeout)
        b = breaker(self.source)
        if not b.allow():
            raise CircuitOpen(f"{self.source} is temporarily unavailable (circuit open)")
        try:
            resp = super().request(method, url, *args, **kwargs)
        except requests.RequestException:
            b.record_failure()
            raise
        if resp.status_code == 429 or resp.status_code >= 500:
            b.record_failure()
        else:
            b.record_success()
        return resp


def session(source: str, **kwargs: Any) -> GuardedSession:
    return GuardedSession(source, **kwargs)


def get(url: str, source: str, **kwargs: Any) -> requests.Response:
    with session(source) as s:
        return s.get(url, **kwargs)
