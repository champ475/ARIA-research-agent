"""
utils/jobs.py — one owner per background job, with a heartbeat (REL-05).

Every scheduled job (alert check, housekeeping, nightly refreshes) and the
Telegram long-poller runs under a Postgres advisory lock named after the
job, taken on a dedicated connection. However many processes try — two
`worker.py` replicas, app replicas running jobs in-process, the GitHub
Actions alert check — one holds the lock and the rest skip. Telegram allows
a single getUpdates poller per bot; two used to fight with 409s.

Whether a job is due is decided from its `job_heartbeats` row (when it last
started), not from process memory, so a job another process just ran isn't
run again here. Each run records start, finish, status, duration and owner;
the ops watchdog (OBS-04) alerts when a heartbeat goes stale.

Session advisory locks need a direct Postgres connection: a transaction-mode
pooler (Neon's `-pooler` host, PgBouncer) can hand the lock's session to
someone else. Set JOBS_DATABASE_URL to the direct host when DATABASE_URL is
a pooler.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any

import psycopg

from utils.helpers import IST

log = logging.getLogger(__name__)

LOCK_NAMESPACE = "aria.jobs"
OWNER = f"{socket.gethostname()}:{os.getpid()}"


def _lock_url() -> str:
    from config import JOBS_DATABASE_URL
    from utils import db

    return JOBS_DATABASE_URL or db.DATABASE_URL


@contextlib.contextmanager
def job_lock(name: str) -> Iterator[psycopg.Connection | None]:
    """Yields the lock-holding connection, or None when another process owns
    `name`. The lock is released (and the connection closed) on exit — also
    if this process dies, since Postgres drops a closed session's locks."""
    from utils.db import connection_kwargs

    kwargs = {**connection_kwargs(), "application_name": f"aria-job:{name}"[:63]}
    kwargs.pop("options", None)  # no statement_timeout on a lock-holding session
    conn = psycopg.connect(_lock_url(), autocommit=True, **kwargs)
    got = False
    try:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s)) AS ok", (LOCK_NAMESPACE, name)
        ).fetchone()
        got = bool(row and row["ok"])
        yield conn if got else None
    finally:
        if got:
            with contextlib.suppress(Exception):
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", (LOCK_NAMESPACE, name))
        conn.close()


@dataclass(frozen=True)
class Job:
    """`every`: run when the last start is at least this old. `daily_at`:
    once a day at or after this IST time. `when`: an extra gate on the IST
    clock (e.g. market hours, trading days)."""

    name: str
    fn: Callable[[], Any]
    every: timedelta | None = None
    daily_at: dtime | None = None
    when: Callable[[datetime], bool] | None = None
    tags: tuple[str, ...] = field(default=())


def is_due(job: Job, last_started: datetime | None, now: datetime) -> bool:
    now = now.astimezone(IST)
    if job.when is not None and not job.when(now):
        return False
    if job.every is not None:
        return last_started is None or now - last_started.astimezone(IST) >= job.every
    if job.daily_at is not None:
        # Never before today's slot, even on a first start.
        slot = IST.localize(datetime.combine(now.date(), job.daily_at))
        return now >= slot and (last_started is None or last_started.astimezone(IST) < slot)
    return False


def _last_started(name: str) -> datetime | None:
    from utils.db import job_heartbeats

    row = next((r for r in job_heartbeats() if r["job"] == name), None)
    return row["last_started_at"] if row else None


def run_job(job: Job, now: datetime | None = None, *, check_due: bool = True) -> str:
    """'ran' | 'failed' | 'skipped' (another owner holds it) | 'not_due'."""
    from utils.db import job_heartbeat_finish, job_heartbeat_start

    with job_lock(job.name) as lock:
        if lock is None:
            return "skipped"
        # Re-checked under the lock: another owner may have just run it.
        if check_due and not is_due(job, _last_started(job.name), now or datetime.now(IST)):
            return "not_due"
        job_heartbeat_start(job.name, OWNER)
        started = time.monotonic()
        error = None
        try:
            job.fn()
        except Exception as e:
            error = type(e).__name__
            log.exception("[jobs] %s failed", job.name)
        job_heartbeat_finish(job.name, error is None, int((time.monotonic() - started) * 1000), error)
        return "ran" if error is None else "failed"


def run_service(
    name: str,
    step: Callable[[], Any],
    stop: threading.Event,
    beat_every: float = 60.0,
    standby_wait: float = 60.0,
) -> None:
    """Run `step` in a loop for as long as this process owns `name` (e.g. the
    Telegram poller). A standby process retries the lock every
    `standby_wait` seconds, so it takes over if the owner dies."""
    from utils.db import job_heartbeat_finish, job_heartbeat_start

    while not stop.is_set():
        try:
            with job_lock(name) as lock:
                if lock is None:
                    stop.wait(standby_wait)
                    continue
                log.info("[jobs] %s: this process is the owner", name)
                job_heartbeat_start(name, OWNER)
                last_beat = time.monotonic()
                failed = 0
                while not stop.is_set():
                    try:
                        step()
                        failed = 0
                    except Exception as e:
                        failed += 1
                        log.warning("[jobs] %s step failed: %s", name, type(e).__name__)
                        stop.wait(min(60.0, 2.0**failed))
                    if time.monotonic() - last_beat >= beat_every:
                        job_heartbeat_finish(name, failed == 0, int((time.monotonic() - last_beat) * 1000))
                        last_beat = time.monotonic()
                    lock.execute("SELECT 1")  # the lock lives with this session; notice if it died
        except Exception as e:
            log.warning("[jobs] %s lost its database session (%s); retrying", name, type(e).__name__)
            stop.wait(standby_wait)
