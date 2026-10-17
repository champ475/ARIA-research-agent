"""
utils/retention.py — keep every table inside its retention period (DATA-10, COMP-06).

One schedule (`SCHEDULE`) lists each data class, how long it is kept and how it
is purged. `run_retention()` applies it (the worker's nightly `retention` job);
`report()` says, per class, the oldest row still held and whether it is inside
its limit. Purges run in bounded batches so one run never holds a long lock or
trips the statement timeout.

Deleting a user's account removes their rows separately (utils/db.py
delete_account); this module is about *time*, not about people.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

MAX_BATCHES = 40  # per class per run, so the job always ends


@dataclass(frozen=True)
class Rule:
    name: str
    keep: timedelta
    purge: Callable[[datetime], int]  # delete one batch older than the cutoff; rows deleted
    oldest: Callable[[], datetime | None]
    batch: int = 5000  # purge() returning fewer than this means done
    note: str = ""


def _table_rule(
    name: str, table: str, column: str, keep: timedelta, extra_where: str = "", note: str = ""
) -> Rule:
    from utils import db

    return Rule(
        name,
        keep,
        lambda cutoff: db.purge_older_than(table, column, cutoff, extra_where),
        lambda: db.oldest_row(table, column, extra_where),
        batch=db._PURGE_BATCH,
        note=note,
    )


def _checkpoint_rule(keep: timedelta) -> Rule:
    from utils import db

    return Rule(
        "langgraph_checkpoints",
        keep,
        lambda cutoff: db.checkpoints_purge(cutoff, batch=200),
        db.checkpoints_oldest,
        batch=200,
        note="threads a chat is paused on (pending interrupt) are kept",
    )


def schedule() -> list[Rule]:
    """The retention schedule. Built on call so importing this module needs no database."""
    return [
        _table_rule(
            "evidence_cache", "evidence_cache", "fetched_at", timedelta(days=7), note="longest TTL is 7 days"
        ),
        _table_rule("pdf_cache", "pdf_cache", "fetched_at", timedelta(days=30)),
        _table_rule("ohlcv_cache", "ohlcv_cache", "fetched_at", timedelta(days=7)),
        _checkpoint_rule(timedelta(days=7)),
        _table_rule("auth_attempts", "auth_attempts", "attempted_at", timedelta(days=1)),
        _table_rule("telegram_link_codes", "telegram_link_codes", "created_at", timedelta(days=1)),
    ]


def run_retention(now: datetime | None = None, rules: list[Rule] | None = None) -> dict[str, int]:
    """Apply every rule; returns rows (threads, for checkpoints) deleted per class.
    A failing class is logged and skipped, so one bad table never stops the rest."""
    now = now or datetime.now(UTC)
    counts: dict[str, int] = {}
    for rule in rules if rules is not None else schedule():
        total = 0
        try:
            cutoff = now - rule.keep
            for _ in range(MAX_BATCHES):
                n = rule.purge(cutoff)
                total += n
                if n < rule.batch:
                    break
        except Exception:
            log.warning("[retention] %s failed", rule.name, exc_info=True)
        counts[rule.name] = total
    log.info("[retention] purged %s", {k: v for k, v in counts.items() if v} or "nothing")
    return counts


def report(now: datetime | None = None, rules: list[Rule] | None = None) -> list[dict[str, Any]]:
    """Per class: limit, oldest row held, age in days and whether it is within its limit."""
    now = now or datetime.now(UTC)
    rows = []
    for rule in rules if rules is not None else schedule():
        try:
            oldest = rule.oldest()
        except Exception:
            log.warning("[retention] cannot read oldest row of %s", rule.name, exc_info=True)
            rows.append({"class": rule.name, "keep_days": rule.keep.days, "oldest": None, "ok": None})
            continue
        if oldest is not None and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        age = (now - oldest).total_seconds() / 86400 if oldest else None
        # one purge interval of grace: the job runs once a day
        ok = True if age is None else age <= rule.keep.total_seconds() / 86400 + 1
        rows.append(
            {
                "class": rule.name,
                "keep_days": rule.keep.days,
                "oldest": oldest.isoformat() if oldest else None,
                "age_days": None if age is None else round(age, 1),
                "ok": ok,
                "note": rule.note,
            }
        )
    return rows
