"""UX-06 on a real Postgres: the Alerts tab's operations: the listing, pause and resume,
delete, edits validated by the same rules as creation, tenant isolation, and the
checker stamping the last check."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from scripts import check_alerts as ca
from services import alerts as svc

pytestmark = pytest.mark.integration


def _alert(db, user, symbol="TCS", atype="price_above", threshold=4000.0, **kw):
    return db.alert_add(user, symbol, atype, f"{symbol} {atype}", threshold, **kw)


def test_rows_show_condition_status_and_delivery(db, two_users):
    a, b = two_users
    aid = _alert(db, a)
    [row] = svc.rows(a)
    assert row["id"] == aid and row["status"] == "Active" and row["delivery"] == "—"
    assert row["last_fired"] == "—" and row["last_checked"] == "—"
    assert svc.rows(b) == []
    db._query(
        "UPDATE alerts SET active=FALSE, triggered_at=now(), delivery_status='pending_delivery' WHERE id=%s",
        (aid,),
        fetch=None,
        commit=True,
    )
    [row] = svc.rows(a)
    assert row["status"] == "Fired (one-time)" and row["delivery"] == "Waiting in your inbox"


def test_pause_resume_delete_only_touch_your_own_alerts(db, two_users):
    a, b = two_users
    aid = _alert(db, a)
    assert svc.set_active(b, aid, False) is False and svc.rows(a)[0]["active"] is True
    assert svc.delete(b, aid) is False
    assert svc.set_active(a, aid, False) is True and svc.rows(a)[0]["status"] == "Paused"
    assert svc.set_active(a, aid, True) is True and svc.rows(a)[0]["status"] == "Active"
    assert svc.delete(a, aid) is True and svc.rows(a) == []


def test_resume_refuses_to_duplicate_an_active_alert(db, two_users):
    a, _ = two_users
    first = _alert(db, a)
    svc.set_active(a, first, False)
    _alert(db, a)  # the same alert, created again while the first is paused
    with pytest.raises(svc.DuplicateAlert):
        svc.set_active(a, first, True)


def test_edit_uses_the_creation_rules(db, two_users):
    a, b = two_users
    aid = _alert(db, a)
    assert "5,000" in svc.edit(a, aid, "5000")
    assert db.alert_get(a, aid)["threshold"] == Decimal(5000)
    for bad in ("-5", "0", "abc", "999999999"):
        with pytest.raises(svc.UserInputError):
            svc.edit(a, aid, bad)
    assert db.alert_get(a, aid)["threshold"] == Decimal(5000)
    with pytest.raises(LookupError):
        svc.edit(b, aid, "1234")  # someone else's alert
    assert db.alert_get(a, aid)["threshold"] == Decimal(5000)


def test_edit_percent_alert_and_recurring_flag(db, two_users):
    a, _ = two_users
    aid = _alert(db, a, "INFY", "pct_change", 5.0, direction="down")
    assert "falls" in svc.edit(a, aid, "7", recurring=True)
    row = db.alert_get(a, aid)
    assert row["threshold"] == Decimal(7) and row["recurring"] is True and row["direction"] == "down"
    with pytest.raises(svc.UserInputError):
        svc.edit(a, aid, "80")  # above the 50% cap


def test_edit_that_would_duplicate_another_alert_is_refused(db, two_users):
    a, _ = two_users
    _alert(db, a, threshold=4000.0)
    other = _alert(db, a, threshold=5000.0)
    with pytest.raises(svc.DuplicateAlert):
        svc.edit(a, other, "4000")
    assert db.alert_get(a, other)["threshold"] == Decimal(5000)


def test_the_checker_stamps_last_checked(db, two_users):
    a, _ = two_users
    aid = _alert(db, a)
    paused = _alert(db, a, "INFY")
    svc.set_active(a, paused, False)
    conn = db.get_conn()
    try:
        with patch.object(ca.RunCache, "info", lambda self, sym: {"currentPrice": 100.0}):
            ca.run_once(conn)
    finally:
        db.release_conn(conn)
    rows = {r["id"]: r for r in svc.rows(a)}
    assert rows[aid]["last_checked"] != "—"
    assert rows[paused]["last_checked"] == "—"  # paused alerts are not evaluated
