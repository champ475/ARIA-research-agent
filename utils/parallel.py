"""
utils/parallel.py — run independent I/O calls side by side with a time budget (REL-07).

    results = parallel_map(fetch_one, symbols, max_workers=8, budget_s=15)
    for ok, value in results: ...        # same order as the input

Each result is `(True, value)` or `(False, exception)`: one slow or failing item
never fails the others. Items still running when the budget ends come back as
`(False, TimeoutError)` and are not waited for (their threads finish on their
own; the network calls underneath carry their own timeouts). The caller's
logging context (turn id, user) is copied into every worker.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(
    fn: Callable[[T], R],
    items: Sequence[T],
    *,
    max_workers: int = 8,
    budget_s: float | None = None,
) -> list[tuple[bool, R | BaseException]]:
    if not items:
        return []
    pool = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(items))), thread_name_prefix="aria-par")
    try:
        futures = [pool.submit(contextvars.copy_context().run, fn, item) for item in items]
        wait(futures, timeout=budget_s)
        out: list[tuple[bool, Any]] = []
        for f in futures:
            if not f.done():
                f.cancel()
                out.append((False, TimeoutError(f"not finished within {budget_s}s")))
            elif f.exception() is not None:
                out.append((False, f.exception()))
            else:
                out.append((True, f.result()))
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
