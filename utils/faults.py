"""
utils/faults.py — switchable faults for the offline fake modes (TEST-06).

The load and chaos runs (loadtests/) drive a server that uses the fake LLM and
the fake market data (ARIA_FAKE_LLM / ARIA_FAKE_DATA), which refuse to start in
production. To make those fakes behave like real, slow, flaky providers, and to
change that while a run is in flight, they read a small JSON file named by
`ARIA_FAULTS_FILE`, re-read at most once a second:

    {"llm_latency_ms": 400,          # every LLM call waits this long
     "llm_first_token_ms": 600,      # a streamed answer's first token waits this long
     "llm_error_rate": 0.0,          # fraction of LLM calls that fail
     "data_latency_ms": 150,         # every market-data call waits this long
     "data_error_rate": 0.0}         # fraction of market-data calls that raise (an upstream 500)

With no file, or no such key, nothing is injected. This module never reads a
database or the network, and nothing outside the fakes calls it.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any

import config

_lock = threading.Lock()
_cache: tuple[float, float, dict[str, Any]] = (0.0, 0.0, {})  # (checked at, file mtime, values)


def _values() -> dict[str, Any]:
    global _cache
    path = config.ARIA_FAULTS_FILE.strip()
    if not path:
        return {}
    now = time.monotonic()
    with _lock:
        checked, mtime, values = _cache
        if now - checked < 1.0:
            return values
        try:
            m = Path(path).stat().st_mtime
            if m != mtime:
                values = json.loads(Path(path).read_text(encoding="utf-8")) or {}
                mtime = m
        except (OSError, ValueError):
            values, mtime = {}, 0.0
        _cache = (now, mtime, values)
        return values


def _num(key: str) -> float:
    try:
        return max(0.0, float(_values().get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def llm_call() -> None:
    """Before a fake LLM call: wait, and sometimes fail like an unavailable provider."""
    delay = _num("llm_latency_ms")
    if delay:
        time.sleep(delay / 1000)
    if random.random() < _num("llm_error_rate"):  # noqa: S311  # nosec B311 - fault injection, not security
        raise RuntimeError("injected LLM failure")


def llm_first_token() -> None:
    delay = _num("llm_first_token_ms")
    if delay:
        time.sleep(delay / 1000)


def data_call() -> None:
    """Before a fake market-data call: wait, and sometimes raise like an upstream 500."""
    delay = _num("data_latency_ms")
    if delay:
        time.sleep(delay / 1000)
    if random.random() < _num("data_error_rate"):  # noqa: S311  # nosec B311
        raise ConnectionError("injected upstream 500")


def reset() -> None:
    global _cache
    with _lock:
        _cache = (0.0, 0.0, {})
