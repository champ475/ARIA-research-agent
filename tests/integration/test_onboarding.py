"""UX-01 on a real Postgres: a new account sees the first-run card until it
dismisses it; the card's trial count is today's."""

from __future__ import annotations

from datetime import date

import pytest

pytestmark = pytest.mark.integration


def test_new_user_until_dismissed(db, two_users):
    a, b = two_users
    today = date(2026, 10, 16)
    db._query("UPDATE users SET onboarded_at = NULL", fetch=None, commit=True)
    assert db.user_onboarding(a, today) == {
        "onboarded": False,
        "used_today": 0,
        "byo": False,
        "telegram": False,
    }
    db.trial_quota_check_and_increment(a, 5, today)
    db.trial_quota_check_and_increment(a, 5, today)
    assert db.user_onboarding(a, today)["used_today"] == 2
    assert db.user_onboarding(a, date(2026, 10, 17))["used_today"] == 0  # a new day
    db.user_mark_onboarded(a)
    assert db.user_onboarding(a, today)["onboarded"] is True
    assert db.user_onboarding(b, today)["onboarded"] is False  # B's card is untouched
