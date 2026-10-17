"""OBS-04 on a real Postgres: each alert rule fires under an injected fault,
pages once with its runbook link, reminds after the reminder interval, and
sends one "resolved" when the fault clears."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from tests.integration.test_slo import _turn
from utils import watchdog
from utils.helpers import IST

pytestmark = pytest.mark.integration

MARKET_OPEN = IST.localize(datetime(2026, 10, 15, 11, 0)).astimezone(UTC)  # a Thursday
MARKET_CLOSED = IST.localize(datetime(2026, 10, 18, 11, 0)).astimezone(UTC)  # a Sunday


@pytest.fixture
def pages():
    sent: list[tuple[str, str]] = []
    with patch.object(
        watchdog, "notify", side_effect=lambda subject, text: sent.append((subject, text)) or True
    ):
        yield sent


def _run(now=None, checks=None):
    return watchdog.run(now or datetime.now(UTC), checks)


def test_error_rate_pages_once_reminds_and_resolves(db, pages):
    for _ in range(17):
        _turn(db, "ok")
    for _ in range(3):
        _turn(db, "error")
    out = _run(checks=[watchdog._error_rate])
    assert out["paged"] == ["error_rate"]
    subject, text = pages[0]
    assert "error rate above 5%" in subject.lower() and "3 of 20 turns" in text
    assert text.endswith("/docs/RUNBOOK.md#error-rate")

    assert _run(checks=[watchdog._error_rate])["paged"] == []  # still firing: no repeat page
    db._query("UPDATE ops_alerts SET last_notified_at = now() - interval '2 hours'", fetch=None, commit=True)
    assert _run(checks=[watchdog._error_rate])["paged"] == ["error_rate"]  # reminder

    db._query("DELETE FROM turns WHERE status = 'error'", fetch=None, commit=True)
    out = _run(checks=[watchdog._error_rate])
    assert out["resolved"] == ["error_rate"] and pages[-1][0].startswith("✅ ARIA resolved")
    assert db.ops_alerts_open() == []


def test_too_few_turns_never_page(db, pages):
    _turn(db, "error")
    assert _run(checks=[watchdog._error_rate])["firing"] == []


def test_slow_equity_answers(db, pages):
    for _ in range(6):
        _turn(db, "ok", ttft_ms=45_000)
    assert _run(checks=[watchdog._latency])["paged"] == ["latency"]
    assert "#latency" in pages[0][1]


def test_stale_alert_check_only_in_market_hours(db, pages):
    assert _run(MARKET_OPEN, [watchdog._jobs])["paged"] == ["heartbeat_stale"]
    assert "No successful alert check at all yet" in pages[0][1]
    db.ops_alert_resolve("heartbeat_stale")
    assert _run(MARKET_CLOSED, [watchdog._jobs])["firing"] == []
    db.job_heartbeat_start("alert_check", "test")
    db.job_heartbeat_finish("alert_check", True, 10)
    db._query(
        "UPDATE job_heartbeats SET last_ok_at=%s",
        (MARKET_OPEN - timedelta(minutes=3),),
        fetch=None,
        commit=True,
    )
    assert _run(MARKET_OPEN, [watchdog._jobs])["firing"] == []


def test_jobs_and_canary_failing_twice(db, pages):
    for job in ("mf_refresh", "data_canary"):
        db.job_heartbeat_start(job, "test")
        db.job_heartbeat_finish(job, False, 5, "ConnectionError")
    assert _run(MARKET_CLOSED, [watchdog._jobs])["firing"] == []  # once isn't a pattern
    for job in ("mf_refresh", "data_canary"):
        db.job_heartbeat_start(job, "test")
        db.job_heartbeat_finish(job, False, 5, "ConnectionError")
    out = _run(MARKET_CLOSED, [watchdog._jobs])
    assert sorted(out["paged"]) == ["canary_failing", "job_failing:mf_refresh"]
    db.job_heartbeat_start("mf_refresh", "test")
    db.job_heartbeat_finish("mf_refresh", True, 5)
    assert _run(MARKET_CLOSED, [watchdog._jobs])["resolved"] == ["job_failing:mf_refresh"]


def test_llm_exhaustion(db, pages):
    for _ in range(3):
        _turn(db, "llm_unavailable")
    assert _run(checks=[watchdog._llm])["paged"] == ["llm_exhausted"]


def test_database_size_and_connections(db, pages, monkeypatch):
    monkeypatch.setattr("config.DB_SIZE_LIMIT_MB", 1)  # any real database is > 0.8 MB
    assert _run(checks=[watchdog._database])["paged"] == ["db_size"]
    with patch(
        "utils.db.db_usage", return_value={"connections": 90, "max_connections": 100, "size_bytes": 0}
    ):
        assert "db_connections" in _run(checks=[watchdog._database])["paged"]


def test_stale_fundamentals_snapshot(db, pages):
    db.fundamentals_upsert([{"symbol": "TCS", "name": "TCS", "price": 1.0}])
    assert _run(checks=[watchdog._fundamentals])["firing"] == []
    db._query("UPDATE fundamentals_snapshot SET as_of = now() - interval '30 hours'", fetch=None, commit=True)
    assert _run(checks=[watchdog._fundamentals])["paged"] == ["fundamentals_stale"]


def test_a_check_that_cannot_run_resolves_nothing(db, pages):
    db.ops_alert_fire("db_size", "Database above 80% of its size limit", "x", 3600)

    def broken(now):
        raise RuntimeError("db down")

    assert _run(checks=[broken])["resolved"] == []
    assert [r["key"] for r in db.ops_alerts_open()] == ["db_size"]


def test_notify_sends_to_configured_channels(monkeypatch):
    sent = {}
    monkeypatch.setattr("config.OPERATOR_EMAIL", "ops@example.test")
    monkeypatch.setattr("config.OPERATOR_TELEGRAM_CHAT_ID", "")
    with patch("utils.mailer.send", side_effect=lambda to, s, t: sent.update(to=to, s=s) or True):
        assert watchdog.notify("🚨 ARIA: x", "detail")
    assert sent == {"to": "ops@example.test", "s": "🚨 ARIA: x"}
    monkeypatch.setattr("config.OPERATOR_EMAIL", "")
    assert watchdog.notify("🚨 ARIA: x", "detail") is False  # nowhere to send: logged only
