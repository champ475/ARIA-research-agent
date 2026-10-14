"""REL-02: circuit breakers, bounded waits, the guarded HTTP session, and the
yfinance wrapper's fallback when the source stalls or is tripped open."""

import contextvars
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from utils import resilience
from utils.resilience import CallTimeout, CircuitBreaker, CircuitOpen, call_with_timeout, guarded_call


@pytest.fixture(autouse=True)
def _fresh_breakers():
    resilience.reset_breakers()
    yield
    resilience.reset_breakers()


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ── CircuitBreaker ────────────────────────────────────────────────────────────


def test_breaker_opens_after_threshold_failures_within_window():
    clock = _Clock()
    b = CircuitBreaker("x", failure_threshold=3, window_seconds=10, cooldown_seconds=30, clock=clock)
    for _ in range(2):
        b.record_failure()
    assert b.state == "closed" and b.allow()
    b.record_failure()
    assert b.state == "open" and not b.allow()


def test_failures_outside_the_window_do_not_count():
    clock = _Clock()
    b = CircuitBreaker("x", failure_threshold=3, window_seconds=10, clock=clock)
    b.record_failure()
    b.record_failure()
    clock.t += 11
    b.record_failure()
    assert b.state == "closed"


def test_half_open_lets_exactly_one_trial_through_then_closes_on_success():
    clock = _Clock()
    b = CircuitBreaker("x", failure_threshold=1, cooldown_seconds=30, clock=clock)
    b.record_failure()
    clock.t += 30
    assert b.state == "half_open"
    assert b.allow() is True
    assert b.allow() is False  # the trial is in flight
    b.record_success()
    assert b.state == "closed" and b.allow()


def test_failed_trial_reopens_for_a_full_cooldown():
    clock = _Clock()
    b = CircuitBreaker("x", failure_threshold=1, cooldown_seconds=30, clock=clock)
    b.record_failure()
    clock.t += 30
    assert b.allow()
    b.record_failure()
    assert b.state == "open"
    clock.t += 29
    assert b.state == "open"
    clock.t += 1
    assert b.state == "half_open"


def test_success_resets_the_failure_count():
    b = CircuitBreaker("x", failure_threshold=3)
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert b.state == "closed"


# ── bounded waits ─────────────────────────────────────────────────────────────


def test_call_with_timeout_returns_in_time_and_carries_context():
    var = contextvars.ContextVar("turn", default="none")
    var.set("turn-123")
    assert call_with_timeout(lambda: var.get(), timeout=2) == "turn-123"


def test_call_with_timeout_stops_waiting_on_a_stalled_call():
    release = threading.Event()
    start = time.monotonic()
    with pytest.raises(CallTimeout):
        call_with_timeout(release.wait, 60, timeout=0.3)
    assert time.monotonic() - start < 2
    release.set()  # let the worker finish so the pool doesn't hold a stuck thread


def test_guarded_call_counts_timeouts_and_then_fails_fast():
    release = threading.Event()
    for _ in range(resilience.BREAKER_FAILURES):
        with pytest.raises(CallTimeout):
            guarded_call("slow-src", release.wait, 60, timeout=0.05)
    called = MagicMock()
    with pytest.raises(CircuitOpen):
        guarded_call("slow-src", called)
    called.assert_not_called()
    release.set()


# ── guarded HTTP session ──────────────────────────────────────────────────────


def _resp(status):
    r = requests.Response()
    r.status_code = status
    return r


def test_session_applies_default_timeout_and_trips_on_5xx():
    from utils import http

    with patch.object(requests.Session, "request", return_value=_resp(503)) as req:
        for _ in range(resilience.BREAKER_FAILURES):
            http.get("https://example.test/x", source="flaky")
        assert req.call_args.kwargs["timeout"] == http.DEFAULT_TIMEOUT
        with pytest.raises(CircuitOpen):
            http.get("https://example.test/x", source="flaky")
        assert req.call_count == resilience.BREAKER_FAILURES  # the open breaker never hit the network


def test_session_success_and_4xx_do_not_trip():
    from utils import http

    with patch.object(requests.Session, "request", return_value=_resp(404)):
        for _ in range(resilience.BREAKER_FAILURES + 2):
            http.get("https://example.test/x", source="fine")
    assert resilience.breaker("fine").state == "closed"


def test_session_network_error_counts_as_failure():
    from utils import http

    with patch.object(requests.Session, "request", side_effect=requests.ConnectionError("down")):
        for _ in range(resilience.BREAKER_FAILURES):
            with pytest.raises(requests.ConnectionError):
                http.get("https://example.test/x", source="down")
    assert resilience.breaker("down").state == "open"


# ── yfinance wrapper ──────────────────────────────────────────────────────────


def test_stalled_yfinance_falls_back_to_bhavcopy_within_budget(monkeypatch):
    import tools.data.fetcher as fetcher

    release = threading.Event()

    class _StalledTicker:
        @property
        def info(self):
            release.wait(60)
            return {}

    monkeypatch.setattr(fetcher, "YF_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(fetcher, "_info_cache", {})
    monkeypatch.setattr(fetcher, "_tk", lambda symbol, nse=True: _StalledTicker())
    row = {
        "close": 100.0,
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "prev_close": 97.0,
        "date": "14-Oct-2026",
    }
    monkeypatch.setattr(fetcher, "_bhavcopy_row", lambda s: row)
    start = time.monotonic()
    info = fetcher.get_info("TCS")
    assert time.monotonic() - start < 2  # two bounded attempts (NSE, BSE), not 60 s
    assert info["currentPrice"] == 100.0 and "_degraded_source" in info
    release.set()


def test_open_yfinance_breaker_skips_the_call_entirely(monkeypatch):
    import tools.data.fetcher as fetcher

    for _ in range(resilience.BREAKER_FAILURES):
        resilience.breaker("yfinance").record_failure()
    ticker = MagicMock()
    monkeypatch.setattr(fetcher, "_tk", lambda symbol, nse=True: ticker)
    monkeypatch.setattr(fetcher, "_info_cache", {})
    monkeypatch.setattr(fetcher, "_bhavcopy_row", lambda s: None)
    assert fetcher.get_info("INFY") == {}
    assert not ticker.mock_calls  # .info never touched
