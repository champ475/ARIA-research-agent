"""
utils/watchdog.py — pages the operator when ARIA is unhealthy (OBS-04).

Runs as the `ops_watchdog` worker job every 5 minutes. Each check reads the
shared database (turns, job heartbeats, Postgres stats), never one app
process's memory, so it sees every replica. A firing check sends one
message — to OPERATOR_TELEGRAM_CHAT_ID and/or OPERATOR_EMAIL — with a link to
its section in docs/RUNBOOK.md; while it keeps firing a reminder goes out
every ALERT_REMINDER_MINUTES; when it clears, one "resolved" message.

Rules (docs/RUNBOOK.md has one section per anchor):
  error_rate          > 5% of turns failed over 10 min (≥ 10 turns)
  latency             equity p95 time to first output > 2 × SLO over 15 min (≥ 5 turns)
  heartbeat_stale     the alert check hasn't succeeded for 15 min while the market is open
  job_failing:<job>   a background job failed twice in a row
  canary_failing      the data canary failed twice in a row
  llm_exhausted       ≥ 3 turns in 15 min where every LLM provider failed (keys out of quota?)
  db_connections      > 80% of Postgres connections in use
  db_size             database > 80% of DB_SIZE_LIMIT_MB
  fundamentals_stale  the screener's snapshot is older than FUNDAMENTALS_MAX_AGE_HOURS
  retention_overdue   a table still holds rows older than its retention period (hourly check)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Firing:
    key: str
    title: str
    detail: str
    anchor: str


Check = Callable[[datetime], list[Firing]]


def _error_rate(now: datetime) -> list[Firing]:
    from utils.db import turn_window_stats

    s = turn_window_stats(now - timedelta(minutes=10))
    turns, failed = int(s.get("turns") or 0), int(s.get("failed") or 0)
    if turns >= 10 and failed / turns > 0.05:
        return [
            Firing(
                "error_rate",
                "Chat error rate above 5%",
                f"{failed} of {turns} turns failed in the last 10 minutes ({failed / turns:.0%}).",
                "error-rate",
            )
        ]
    return []


def _latency(now: datetime) -> list[Firing]:
    from config import SLO_EQUITY_P95_TTFT_SECONDS
    from utils.db import turn_window_stats

    s = turn_window_stats(now - timedelta(minutes=15))
    p95 = s.get("equity_p95_ttft_ms")
    limit = 2 * SLO_EQUITY_P95_TTFT_SECONDS
    if int(s.get("equity_turns") or 0) >= 5 and p95 is not None and float(p95) / 1000 > limit:
        return [
            Firing(
                "latency",
                "Equity answers are slow",
                f"p95 time to first output is {float(p95) / 1000:.1f} s over 15 minutes (alert above {limit:.0f} s).",
                "latency",
            )
        ]
    return []


def _jobs(now: datetime) -> list[Firing]:
    from utils.db import job_heartbeats
    from utils.market_clock import is_open

    out: list[Firing] = []
    beats = {r["job"]: r for r in job_heartbeats()}
    alerts = beats.get("alert_check")
    # Only once the market has been open long enough for a run to be due.
    if is_open(now) and is_open(now - timedelta(minutes=15)):
        last_ok = alerts.get("last_ok_at") if alerts else None
        if last_ok is None or now - last_ok > timedelta(minutes=15):
            since = f"since {last_ok.astimezone(UTC):%H:%M} UTC" if last_ok else "at all yet"
            out.append(
                Firing(
                    "heartbeat_stale",
                    "Alert checks have stopped",
                    f"No successful alert check {since} during market hours.",
                    "heartbeat-stale",
                )
            )
    for job, row in beats.items():
        if int(row.get("consecutive_failures") or 0) < 2:
            continue
        if job == "data_canary":
            out.append(
                Firing(
                    "canary_failing",
                    "Data canary failing",
                    f"The live data canary failed {row['consecutive_failures']} times in a row "
                    f"(last error: {row.get('last_error') or 'unknown'}).",
                    "canary-failing",
                )
            )
        else:
            out.append(
                Firing(
                    f"job_failing:{job}",
                    f"Background job '{job}' is failing",
                    f"{row['consecutive_failures']} failures in a row (last error: {row.get('last_error') or 'unknown'}).",
                    "job-failing",
                )
            )
    return out


def _llm(now: datetime) -> list[Firing]:
    from utils.db import turn_window_stats

    n = int(turn_window_stats(now - timedelta(minutes=15)).get("llm_unavailable") or 0)
    if n >= 3:
        return [
            Firing(
                "llm_exhausted",
                "LLM providers unavailable",
                f"{n} turns in 15 minutes got no answer because every LLM provider failed — trial keys "
                "out of quota, retired models or a provider outage.",
                "llm-exhausted",
            )
        ]
    return []


def _database(now: datetime) -> list[Firing]:
    from config import DB_SIZE_LIMIT_MB
    from utils.db import db_usage

    u = db_usage()
    out: list[Firing] = []
    conns, cap = int(u.get("connections") or 0), int(u.get("max_connections") or 0)
    if cap and conns / cap > 0.8:
        out.append(
            Firing(
                "db_connections",
                "Database connections above 80%",
                f"{conns} of {cap} connections in use.",
                "db-connections",
            )
        )
    size_mb = int(u.get("size_bytes") or 0) / 1_048_576
    if size_mb > 0.8 * DB_SIZE_LIMIT_MB:
        out.append(
            Firing(
                "db_size",
                "Database above 80% of its size limit",
                f"{size_mb:.0f} MB of {DB_SIZE_LIMIT_MB} MB.",
                "db-size",
            )
        )
    return out


def _fundamentals(now: datetime) -> list[Firing]:
    from config import FUNDAMENTALS_MAX_AGE_HOURS
    from utils.db import fundamentals_status

    s = fundamentals_status()
    oldest = s.get("oldest")
    if s.get("rows") and oldest is not None:
        age_h = (now - oldest).total_seconds() / 3600
        if age_h > FUNDAMENTALS_MAX_AGE_HOURS:
            return [
                Firing(
                    "fundamentals_stale",
                    "Screener snapshot is stale",
                    f"The oldest fundamentals row is {age_h:.0f} hours old (limit {FUNDAMENTALS_MAX_AGE_HOURS} h).",
                    "fundamentals-stale",
                )
            ]
    return []


def _retention(now: datetime) -> list[Firing]:
    if now.minute >= 5:  # the scans are cheap but not free: once an hour is plenty
        return []
    from utils.retention import report

    late = [r for r in report(now) if r["ok"] is False]
    if not late:
        return []
    detail = "; ".join(f"{r['class']}: oldest row {r['age_days']} days, limit {r['keep_days']}" for r in late)
    return [
        Firing("retention_overdue", "Data is older than its retention period", detail, "retention-overdue")
    ]


CHECKS: list[Check] = [_error_rate, _latency, _jobs, _llm, _database, _fundamentals, _retention]


# ── notification ──────────────────────────────────────────────────────────────


def _telegram(text: str) -> bool:
    from config import OPERATOR_TELEGRAM_CHAT_ID, TELEGRAM_BOT_TOKEN
    from utils import http

    if not (OPERATOR_TELEGRAM_CHAT_ID and TELEGRAM_BOT_TOKEN):
        return False
    try:
        r = http.session("telegram").post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": OPERATOR_TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
        )
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception as e:
        log.warning("[watchdog] Telegram page failed: %s", type(e).__name__)
        return False


def _email(subject: str, text: str) -> bool:
    from config import OPERATOR_EMAIL
    from utils import mailer

    return bool(OPERATOR_EMAIL) and mailer.send(OPERATOR_EMAIL, subject, text)


def notify(subject: str, text: str) -> bool:
    """Page the operator on every configured channel; True if any delivered.
    With neither channel configured it only logs (dev)."""
    sent = [_telegram(f"{subject}\n{text}"), _email(subject, text)]
    if not any(sent):
        log.warning("[watchdog] %s — %s (no operator channel delivered)", subject, text)
    return any(sent)


def run(now: datetime | None = None, checks: list[Check] | None = None) -> dict[str, list[str]]:
    """Evaluate every check once; page new/reminder alerts and resolutions."""
    from config import ALERT_REMINDER_MINUTES, RUNBOOK_URL
    from utils.db import ops_alert_fire, ops_alert_resolve, ops_alerts_open

    now = now or datetime.now(UTC)
    firing: list[Firing] = []
    check_failed = False
    for check in checks or CHECKS:
        try:
            firing.extend(check(now))
        except Exception:
            check_failed = True
            log.exception("[watchdog] check %s failed", getattr(check, "__name__", check))
    paged: list[str] = []
    for f in firing:
        if ops_alert_fire(f.key, f.title, f.detail, ALERT_REMINDER_MINUTES * 60):
            notify(f"🚨 ARIA: {f.title}", f"{f.detail}\nRunbook: {RUNBOOK_URL}#{f.anchor}")
            paged.append(f.key)
    resolved: list[str] = []
    if not check_failed:  # a check that couldn't run can't say its alert cleared
        live = {f.key for f in firing}
        for row in ops_alerts_open():
            if row["key"] not in live and ops_alert_resolve(row["key"]):
                notify(f"✅ ARIA resolved: {row['title']}", "Back to normal.")
                resolved.append(row["key"])
    return {"firing": [f.key for f in firing], "paged": paged, "resolved": resolved}
