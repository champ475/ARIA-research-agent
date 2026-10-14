"""Tests for scripts/check_alerts.py — recurring-alert cooldown gating."""

from datetime import UTC, datetime, timedelta

from scripts.check_alerts import in_cooldown


def test_one_shot_alert_never_in_cooldown():
    alert = {"recurring": False, "triggered_at": datetime.now(UTC)}
    assert in_cooldown(alert) is False


def test_recurring_never_triggered_not_in_cooldown():
    alert = {"recurring": True, "triggered_at": None, "cooldown_hours": 24}
    assert in_cooldown(alert) is False


def test_recurring_recently_triggered_is_in_cooldown():
    alert = {
        "recurring": True,
        "triggered_at": datetime.now(UTC) - timedelta(hours=1),
        "cooldown_hours": 24,
    }
    assert in_cooldown(alert) is True


def test_recurring_past_cooldown_window_fires_again():
    alert = {
        "recurring": True,
        "triggered_at": datetime.now(UTC) - timedelta(hours=25),
        "cooldown_hours": 24,
    }
    assert in_cooldown(alert) is False


def test_naive_datetime_triggered_at_handled():
    """Postgres driver may return a naive datetime — must not crash on tz-aware subtraction."""
    alert = {
        "recurring": True,
        "triggered_at": datetime.now() - timedelta(hours=1),
        "cooldown_hours": 24,
    }
    assert in_cooldown(alert) is True


def test_default_cooldown_used_when_missing():
    alert = {"recurring": True, "triggered_at": datetime.now(UTC) - timedelta(hours=1)}
    assert in_cooldown(alert) is True


# ── DATA-08: alerts are consumed only after delivery ──────────────────────────
from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from scripts import check_alerts as ca  # noqa: E402


class _Cur:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows=None):
        self.cur = _Cur(rows)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _alert(**kw):
    base = {
        "id": 1,
        "user_id": 7,
        "symbol": "TCS",
        "alert_type": "price_above",
        "threshold": 100.0,
        "recurring": False,
        "triggered_at": None,
        "telegram_chat_id": "555",
        "delivery_attempts": 0,
    }
    base.update(kw)
    return base


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.text = str(body)
    r.json.return_value = body if body is not None else {"ok": True}
    return r


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(ca, "TELEGRAM_BOT_TOKEN", "123:abc")


def _sql(cur, needle):
    return [(s, p) for s, p in cur.executed if needle in s]


def test_unlinked_user_gets_inbox_entry_before_alert_is_consumed(bot):
    cur = _Cur()
    assert ca.deliver(cur, _alert(telegram_chat_id=None), "msg") == "in_app"
    statements = [s for s, _ in cur.executed]
    insert_at = next(i for i, s in enumerate(statements) if s.startswith("INSERT INTO notifications"))
    update_at = next(i for i, s in enumerate(statements) if "active=FALSE" in s)
    assert insert_at < update_at
    assert _sql(cur, "INSERT INTO notifications")[0][1] == (7, 1, "msg")


def test_missing_bot_token_falls_back_to_inbox(monkeypatch):
    monkeypatch.setattr(ca, "TELEGRAM_BOT_TOKEN", "")
    cur = _Cur()
    assert ca.deliver(cur, _alert(), "msg") == "in_app"


def test_telegram_500_leaves_alert_active_and_counts_attempt(bot):
    cur = _Cur()
    with patch.object(ca.requests, "post", return_value=_resp(500, {"ok": False})):
        assert ca.deliver(cur, _alert(), "msg") == "retry"
    assert not _sql(cur, "active=FALSE")
    assert not _sql(cur, "triggered_at=now()")
    (stmt,) = _sql(cur, "delivery_attempts=%s")
    assert stmt[1][0] == 1 and stmt[1][1] == "http_500"


def test_retry_then_success_on_next_run(bot):
    cur = _Cur()
    with patch.object(ca.requests, "post", return_value=_resp(200, {"ok": True})):
        assert ca.deliver(cur, _alert(delivery_attempts=1), "msg") == "telegram"
    (stmt,) = _sql(cur, "active=FALSE")
    assert stmt[1] == ("telegram", 1)


def test_ok_false_in_a_200_body_is_a_failure(bot):
    cur = _Cur()
    with patch.object(
        ca.requests, "post", return_value=_resp(200, {"ok": False, "description": "chat not found"})
    ):
        assert ca.deliver(cur, _alert(), "msg") == "retry"


def test_network_exception_is_a_retry(bot):
    cur = _Cur()
    with patch.object(ca.requests, "post", side_effect=ConnectionError("boom")):
        assert ca.deliver(cur, _alert(), "msg") == "retry"


def test_after_max_attempts_falls_back_to_inbox(bot):
    cur = _Cur()
    with patch.object(ca.requests, "post", return_value=_resp(502, {})):
        outcome = ca.deliver(cur, _alert(delivery_attempts=ca.ALERT_MAX_DELIVERY_ATTEMPTS - 1), "msg")
    assert outcome == "in_app"
    assert _sql(cur, "INSERT INTO notifications") and _sql(cur, "active=FALSE")


def test_recurring_alert_is_not_deactivated_on_delivery(bot):
    cur = _Cur()
    with patch.object(ca.requests, "post", return_value=_resp()):
        ca.deliver(cur, _alert(recurring=True), "msg")
    assert not _sql(cur, "active=FALSE")
    assert _sql(cur, "triggered_at=now()")


def test_ten_alerts_on_one_symbol_fetch_once(bot):
    rows = [_alert(id=i, threshold=float(i)) for i in range(1, 11)]
    conn = _Conn(rows)
    with (
        patch.object(ca, "get_info", return_value={"currentPrice": 50.0}) as gi,
        patch.object(ca.requests, "post", return_value=_resp()),
    ):
        counts = ca.run_once(conn)
    assert gi.call_count == 1
    assert counts["telegram"] == 10


def test_fetch_failure_is_memoised_per_run(bot):
    rows = [_alert(id=i) for i in range(1, 6)]
    with patch.object(ca, "get_info", side_effect=RuntimeError("yfinance down")) as gi:
        counts = ca.run_once(_Conn(rows))
    assert gi.call_count == 1
    assert counts["errors"] == 5 and counts["fired"] == 0


def test_indicator_alerts_share_one_ohlcv_fetch(bot):
    import numpy as np
    import pandas as pd

    idx = pd.date_range("2025-01-01", periods=60, freq="D")
    df = pd.DataFrame(
        {
            "Open": np.linspace(1, 2, 60),
            "High": np.linspace(1, 2, 60),
            "Low": np.linspace(1, 2, 60),
            "Close": np.linspace(1, 2, 60),
            "Volume": np.full(60, 1000.0),
        },
        index=idx,
    )
    rows = [
        _alert(id=1, alert_type="rsi_cross_above", threshold=50.0),
        _alert(id=2, alert_type="macd_bullish_cross", threshold=None),
        _alert(id=3, alert_type="bb_breakout", threshold=None),
    ]
    with patch.object(ca, "get_ohlcv", return_value=df) as go:
        ca.run_once(_Conn(rows))
    assert go.call_count == 1


def test_run_applies_delivery_schema_first(bot):
    conn = _Conn([])
    ca.run_once(conn)
    assert "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS delivery_attempts" in conn.cur.executed[0][0]


def test_inbox_markdown_escapes_and_marks_unread(app_module):
    from datetime import datetime

    rows = [
        {
            "message": "<b>TCS</b> above ₹100",
            "created_at": datetime(2026, 10, 14, 5, 0, tzinfo=UTC),
            "read_at": None,
        },
        {
            "message": "INFY",
            "created_at": datetime(2026, 10, 13, 5, 0, tzinfo=UTC),
            "read_at": datetime.now(UTC),
        },
    ]
    md = app_module._inbox_md(rows)
    assert "&lt;b&gt;TCS" in md and "<b>" not in md
    assert md.splitlines()[0].startswith("- 🔵") and not md.splitlines()[1].startswith("- 🔵")
    assert "10:30 IST" in md
    assert app_module._inbox_md([]) == "*No alert notifications.*"
