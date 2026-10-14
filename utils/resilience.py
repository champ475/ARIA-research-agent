"""
utils/resilience.py — bounded waits and circuit breakers for external calls (REL-02).

Two problems this solves:
- Some data libraries take no timeout at all (yfinance's .info, feedparser
  given a URL), so one stalled upstream could hang a chat turn for minutes.
  `call_with_timeout` bounds how long the *caller* waits: the call runs on a
  worker thread and the caller gets CallTimeout when time is up. (Python
  can't kill a thread; a stuck worker finishes or dies on its own, and the
  pools are bounded so stuck workers can't grow without limit.)
- A source that is down kept being hammered by every turn. `CircuitBreaker`
  opens after repeated failures, fails fast for a cool-down, then lets one
  trial call through (half-open) before closing again.

`guarded_call(source, fn, ...)` combines both. Workers inherit the caller's
contextvars, so log lines from inside a guarded call keep the turn_id.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

BREAKER_FAILURES = int(os.getenv("BREAKER_FAILURES", "5"))
BREAKER_WINDOW_SECONDS = float(os.getenv("BREAKER_WINDOW_SECONDS", "60"))
BREAKER_COOLDOWN_SECONDS = float(os.getenv("BREAKER_COOLDOWN_SECONDS", "30"))


class CallTimeout(TimeoutError):
    """The guarded call didn't finish within its time budget."""


class CircuitOpen(RuntimeError):
    """The source's breaker is open — the call was skipped without trying."""


class CircuitBreaker:
    """Closed -> (N failures within the window) -> open -> (cool-down) ->
    half-open: exactly one trial call; success closes, failure re-opens."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = BREAKER_FAILURES,
        window_seconds: float = BREAKER_WINDOW_SECONDS,
        cooldown_seconds: float = BREAKER_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self._threshold = failure_threshold
        self._window = window_seconds
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._trial_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._cooldown:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """True if a call may proceed now. In half-open, only one caller gets
        True until that trial call reports back."""
        with self._lock:
            state = self._state_locked()
            if state == "closed":
                return True
            if state == "half_open" and not self._trial_in_flight:
                self._trial_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._opened_at is not None:
                log.info(f"[breaker] {self.name} closed again")
            self._failures.clear()
            self._opened_at = None
            self._trial_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            if self._opened_at is not None:
                # The half-open trial failed (or a straggler from before the
                # trip): restart the cool-down.
                self._opened_at = now
                self._trial_in_flight = False
                return
            self._failures.append(now)
            while self._failures and now - self._failures[0] > self._window:
                self._failures.popleft()
            if len(self._failures) >= self._threshold:
                self._opened_at = now
                self._failures.clear()
                log.warning(
                    f"[breaker] {self.name} opened after {self._threshold} failures; "
                    f"skipping it for {self._cooldown:.0f}s"
                )


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def breaker(source: str) -> CircuitBreaker:
    with _breakers_lock:
        b = _breakers.get(source)
        if b is None:
            b = _breakers[source] = CircuitBreaker(source)
        return b


def reset_breakers() -> None:
    """Tests only: forget every breaker's state."""
    with _breakers_lock:
        _breakers.clear()


# Two pools so a tool call (execute_task) waiting on its own source calls can
# never deadlock by exhausting the pool those inner calls need.
_IO_POOL = ThreadPoolExecutor(
    max_workers=int(os.getenv("IO_POOL_WORKERS", "64")), thread_name_prefix="aria-io"
)
_TOOL_POOL = ThreadPoolExecutor(
    max_workers=int(os.getenv("TOOL_POOL_WORKERS", "32")), thread_name_prefix="aria-tool"
)


def call_with_timeout(fn: Callable[..., T], *args: Any, timeout: float, pool: str = "io", **kwargs: Any) -> T:
    """Run fn(*args, **kwargs) and wait at most `timeout` seconds for it."""
    if timeout <= 0:
        raise CallTimeout("no time left for this call")
    executor = _TOOL_POOL if pool == "tool" else _IO_POOL
    ctx = contextvars.copy_context()
    future = executor.submit(ctx.run, fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except FutureTimeout as e:
        future.cancel()  # only helps if it never started
        raise CallTimeout(f"{getattr(fn, '__name__', 'call')} took longer than {timeout:.1f}s") from e


def guarded_call(
    source: str, fn: Callable[..., T], *args: Any, timeout: float | None = None, **kwargs: Any
) -> T:
    """Call fn through `source`'s circuit breaker, with an optional time
    budget. Raises CircuitOpen (skipped), CallTimeout, or fn's own error —
    the last two count as failures for the breaker."""
    b = breaker(source)
    if not b.allow():
        raise CircuitOpen(f"{source} is temporarily unavailable (circuit open)")
    try:
        result = call_with_timeout(fn, *args, timeout=timeout, **kwargs) if timeout else fn(*args, **kwargs)
    except Exception:
        b.record_failure()
        raise
    b.record_success()
    return result
