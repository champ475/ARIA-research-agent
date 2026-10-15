"""
utils/maintenance.py — housekeeping that keeps auth tables bounded (SEC-04/05/06).

    run_housekeeping()   delete auth_attempts older than a day, expired login
                         sessions, and verification/reset tokens a day past
                         expiry. Idempotent; safe to run from several places.

server.serve() runs it hourly in a daemon thread; REL-05 moves scheduled jobs
into the single-owner worker.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

HOUSEKEEPING_INTERVAL_SECONDS = 3600
AUTH_ATTEMPT_RETENTION = timedelta(days=1)


def run_housekeeping() -> dict[str, int]:
    from utils import db

    counts = {
        "auth_attempts": db.auth_attempts_purge(datetime.now(UTC) - AUTH_ATTEMPT_RETENTION),
        "sessions": db.auth_sessions_purge_expired(),
        "auth_tokens": db.auth_tokens_purge_expired(),
    }
    log.info("[maintenance] housekeeping purged %s", counts)
    return counts


def start_housekeeping_thread(interval: float = HOUSEKEEPING_INTERVAL_SECONDS) -> threading.Thread:
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(interval):
            try:
                run_housekeeping()
            except Exception:
                log.exception("[maintenance] housekeeping failed")

    t = threading.Thread(target=loop, name="aria-housekeeping", daemon=True)
    t.start()
    return t
