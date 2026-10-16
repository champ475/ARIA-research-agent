"""
utils/maintenance.py — housekeeping that keeps auth tables bounded (SEC-04/05/06).

    run_housekeeping()   delete auth_attempts older than a day, expired login
                         sessions, and verification/reset tokens a day past
                         expiry. Idempotent; safe to run from several places.

worker.py runs it hourly as the `housekeeping` job (REL-05).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

AUTH_ATTEMPT_RETENTION = timedelta(days=1)


def run_housekeeping() -> dict[str, int]:
    from utils import db

    counts = {
        "auth_attempts": db.auth_attempts_purge(datetime.now(UTC) - AUTH_ATTEMPT_RETENTION),
        "sessions": db.auth_sessions_purge_expired(),
        "auth_tokens": db.auth_tokens_purge_expired(),
        "telegram_link_codes": db.telegram_link_codes_purge_expired(),
    }
    log.info("[maintenance] housekeeping purged %s", counts)
    return counts
