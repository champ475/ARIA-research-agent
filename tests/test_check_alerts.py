"""Tests for scripts/check_alerts.py — recurring-alert cooldown gating."""
from datetime import datetime, timedelta, timezone

from scripts.check_alerts import in_cooldown


def test_one_shot_alert_never_in_cooldown():
    alert = {"recurring": False, "triggered_at": datetime.now(timezone.utc)}
    assert in_cooldown(alert) is False


def test_recurring_never_triggered_not_in_cooldown():
    alert = {"recurring": True, "triggered_at": None, "cooldown_hours": 24}
    assert in_cooldown(alert) is False


def test_recurring_recently_triggered_is_in_cooldown():
    alert = {
        "recurring": True,
        "triggered_at": datetime.now(timezone.utc) - timedelta(hours=1),
        "cooldown_hours": 24,
    }
    assert in_cooldown(alert) is True


def test_recurring_past_cooldown_window_fires_again():
    alert = {
        "recurring": True,
        "triggered_at": datetime.now(timezone.utc) - timedelta(hours=25),
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
    alert = {"recurring": True, "triggered_at": datetime.now(timezone.utc) - timedelta(hours=1)}
    assert in_cooldown(alert) is True
