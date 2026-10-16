"""
worker.py — ARIA's background jobs (REL-05).

    python worker.py

Runs every scheduled job when it's due, each under its advisory lock
(utils/jobs.py), plus the Telegram /start-code poller as a lock-held
service. Start as many workers as you like — each job still has exactly one
owner at a time; standbys take over if the owner dies. In dev the app runs
the same scheduler in-process (RUN_JOBS_IN_APP), which the locks also keep
safe alongside a separate worker.

Jobs (IST):
  alert_check            every 5 min while the NSE market is open
  housekeeping           hourly — expired sessions, auth attempts/tokens, link codes
  fundamentals_snapshot  19:30 on trading days — the screener's nightly snapshot
  mf_refresh             23:30 daily — mutual fund metrics after AMFI publishes
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any

from utils.helpers import IST
from utils.jobs import Job, is_due, run_job, run_service

log = logging.getLogger("worker")

TICK_SECONDS = 15.0
MAX_PARALLEL_JOBS = 3


def _alert_check() -> None:
    from scripts import check_alerts
    from utils import db

    conn = db.get_conn()
    try:
        check_alerts.run_once(conn)
    finally:
        db.release_conn(conn)


def _housekeeping() -> None:
    from utils.maintenance import run_housekeeping

    run_housekeeping()


def _fundamentals_snapshot() -> None:
    from scripts.refresh_fundamentals import main

    if main([]) != 0:
        raise RuntimeError("fundamentals refresh wrote nothing")


def _mf_refresh() -> None:
    from scripts.refresh_mf import main

    main([])


def _market_open(now: datetime) -> bool:
    from utils.market_clock import is_open

    return is_open(now)


def _trading_day(now: datetime) -> bool:
    from utils.market_clock import is_trading_day

    return is_trading_day(now.date())


JOBS: list[Job] = [
    Job("alert_check", _alert_check, every=timedelta(minutes=5), when=_market_open),
    Job("housekeeping", _housekeeping, every=timedelta(hours=1)),
    Job("fundamentals_snapshot", _fundamentals_snapshot, daily_at=dtime(19, 30), when=_trading_day),
    Job("mf_refresh", _mf_refresh, daily_at=dtime(23, 30)),
]


class Scheduler:
    """Checks every job each tick and runs the due ones on a small pool
    (a long nightly refresh never delays the 5-minute alert check)."""

    def __init__(self, jobs: list[Job] | None = None, max_parallel: int = MAX_PARALLEL_JOBS):
        self.jobs = list(JOBS if jobs is None else jobs)
        self._pool = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="aria-job")
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def _run(self, job: Job) -> None:
        try:
            outcome = run_job(job)
            if outcome in ("ran", "failed"):
                log.info("[worker] %s: %s", job.name, outcome)
        except Exception as e:  # the lock's database is unreachable, etc.
            log.warning("[worker] %s could not run: %s", job.name, type(e).__name__)
        finally:
            with self._lock:
                self._running.discard(job.name)

    def tick(self, now: datetime | None = None) -> list[str]:
        """Submit every due job not already running here; returns their names."""
        from utils.db import job_heartbeats

        now = now or datetime.now(IST)
        started = {r["job"]: r["last_started_at"] for r in job_heartbeats()}
        submitted = []
        for job in self.jobs:
            with self._lock:
                if job.name in self._running or not is_due(job, started.get(job.name), now):
                    continue
                self._running.add(job.name)
            self._pool.submit(self._run, job)
            submitted.append(job.name)
        return submitted

    def run_forever(self, stop: threading.Event, tick: float = TICK_SECONDS) -> None:
        log.info("[worker] scheduler started (%s)", ", ".join(j.name for j in self.jobs))
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:
                log.warning("[worker] tick failed: %s", type(e).__name__)
            stop.wait(tick)
        self._pool.shutdown(wait=False, cancel_futures=True)


def _telegram_step() -> None:
    from utils.telegram_link import poll_once

    poll_once()


def start(stop: threading.Event) -> list[threading.Thread]:
    """Scheduler + Telegram poller on daemon threads (the app's in-process mode)."""
    from config import TELEGRAM_BOT_TOKEN

    threads = [
        threading.Thread(target=Scheduler().run_forever, args=(stop,), name="aria-scheduler", daemon=True)
    ]
    if TELEGRAM_BOT_TOKEN:
        threads.append(
            threading.Thread(
                target=run_service,
                args=("telegram_poller", _telegram_step, stop),
                name="aria-telegram",
                daemon=True,
            )
        )
    for t in threads:
        t.start()
    return threads


def main() -> int:
    from config import check_boot
    from utils.db import ensure_schema
    from utils.logging_setup import configure_logging

    configure_logging()
    check_boot(serving=False)
    ensure_schema()
    stop = threading.Event()

    def _stop(*_: Any) -> None:
        log.info("[worker] stopping")
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    threads = start(stop)
    while not stop.is_set():
        stop.wait(1.0)
    for t in threads:
        t.join(timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
