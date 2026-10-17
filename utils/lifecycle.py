"""
utils/lifecycle.py — in-flight turn tracking and graceful drain (OPS-04).

On SIGTERM the server stops taking new chat turns (`draining`), readiness
turns 503 so a load balancer stops routing here, and shutdown waits up to
SHUTDOWN_GRACE_SECONDS for the turns already running to finish before the
pools close. A deploy then never cuts off an answer mid-stream.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = 30.0

_lock = threading.Condition()
_in_flight = 0
_draining = threading.Event()


def is_draining() -> bool:
    return _draining.is_set()


def in_flight() -> int:
    with _lock:
        return _in_flight


@contextmanager
def track_turn() -> Iterator[None]:
    """Count one running chat turn for the duration of the block."""
    global _in_flight
    with _lock:
        _in_flight += 1
    try:
        yield
    finally:
        with _lock:
            _in_flight -= 1
            _lock.notify_all()


def begin_drain() -> None:
    if not _draining.is_set():
        log.info("[lifecycle] draining: no new turns; %d in flight", in_flight())
    _draining.set()


def wait_idle(timeout: float = SHUTDOWN_GRACE_SECONDS) -> bool:
    """Block until no turn is running or `timeout` passes. True if idle."""
    deadline = time.monotonic() + timeout
    with _lock:
        while _in_flight > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("[lifecycle] shutdown grace expired with %d turn(s) still running", _in_flight)
                return False
            _lock.wait(remaining)
    return True


def reset() -> None:
    """Tests only."""
    global _in_flight
    with _lock:
        _in_flight = 0
    _draining.clear()
