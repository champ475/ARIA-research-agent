"""DATA-08 on a real Postgres: an alert queued in the in-app inbox stays
active as 'pending_delivery', is not re-queued by later runs, and is consumed
only when its owner reads it."""

from unittest.mock import patch

import pytest

from scripts import check_alerts as ca

pytestmark = pytest.mark.integration


def _alert_row(db, alert_id):
    return db._query("SELECT * FROM alerts WHERE id=%s", (alert_id,), fetch="one")


def _run(db):
    conn = db.get_conn()
    try:
        with (
            patch.object(ca, "TELEGRAM_BOT_TOKEN", ""),
            patch.object(ca, "get_info", return_value={"currentPrice": 4100.0}),
        ):
            return ca.run_once(conn)
    finally:
        db.release_conn(conn)


def test_unlinked_alert_pending_until_read_then_consumed(db, two_users):
    a, b = two_users
    one_shot = db.alert_add(a, "TCS", "price_above", "above", 4000.0)
    recurring = db.alert_add(a, "TCS", "price_above", "above", 3900.0, recurring=True)

    counts = _run(db)
    assert counts["in_app"] == 2
    for alert_id in (one_shot, recurring):
        row = _alert_row(db, alert_id)
        assert row["active"] is True and row["triggered_at"] is None
        assert row["delivery_status"] == "pending_delivery"
    assert db.notifications_unread_count(a) == 2

    # A second run must not queue duplicates while the inbox entries are unread.
    assert _run(db)["fired"] == 0
    assert db.notifications_unread_count(a) == 2

    # Another user's mark-read can't consume A's alerts.
    db.notifications_mark_read(b)
    assert _alert_row(db, one_shot)["delivery_status"] == "pending_delivery"

    db.notifications_mark_read(a)
    row = _alert_row(db, one_shot)
    assert row["active"] is False and row["triggered_at"] is not None
    assert row["delivery_status"] == "in_app"
    row = _alert_row(db, recurring)
    assert row["active"] is True and row["triggered_at"] is not None  # cooldown starts
    assert row["delivery_status"] == "in_app"
