"""COMP-06 on a real Postgres: each user-data class is purged after its period
and not before; deleting a stale chat takes its messages along; the audit log
only lets rows older than five years go; the report and the watchdog see
overdue data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from utils import retention, watchdog

pytestmark = pytest.mark.integration


TURN_IDS = {"old": "00000000-0000-0000-0000-00000000000a", "new": "00000000-0000-0000-0000-00000000000b"}


def _ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _turn(db, turn_id: str, user: int, created: datetime) -> None:
    db._query(
        "INSERT INTO turns (turn_id, user_id, created_at, status, error_id) VALUES (%s,%s,%s,'ok',%s)",
        (turn_id, user, created, turn_id[:8]),
        fetch=None,
        commit=True,
    )


def _chat(db, user: int, title: str, updated: datetime) -> int:
    sid = db._query(
        "INSERT INTO chat_sessions (user_id, tab, title, updated_at) VALUES (%s,'chat',%s,%s) RETURNING id",
        (user, title, updated),
        fetch="one",
        commit=True,
    )["id"]
    db._query(
        "INSERT INTO chat_history (user_id, session_id, tab, role, content) VALUES (%s,%s,'chat','user','hi')",
        (user, sid),
        fetch=None,
        commit=True,
    )
    return sid


def test_chat_history_goes_24_months_after_last_activity(db, two_users):
    a, b = two_users
    _chat(db, a, "stale", _ago(800))
    keep = _chat(db, b, "recent", _ago(700))
    counts = retention.run_retention()
    assert counts["chat_history"] == 1
    assert [r["id"] for r in db._query("SELECT id FROM chat_sessions")] == [keep]
    assert (
        db._query("SELECT count(*) AS n FROM chat_history", fetch="one")["n"] == 1
    )  # cascade took the stale chat's message


def test_each_class_is_purged_only_past_its_period(db, two_users):
    a, _ = two_users
    q = lambda sql, params=(): db._query(sql, params, fetch=None, commit=True)  # noqa: E731
    for age, tag in ((40, "old"), (20, "new")):
        _turn(db, TURN_IDS[tag], a, _ago(age))
    for age in (200, 100):
        q("INSERT INTO notifications (user_id, message, created_at) VALUES (%s,'m',%s)", (a, _ago(age)))
    for age in (800, 600):
        q(
            "INSERT INTO reports (user_id, report_type, title, content, created_at) VALUES (%s,'r','t','c',%s)",
            (a, _ago(age)),
        )
    q("INSERT INTO ops_alerts (key, title, resolved_at) VALUES ('old','t',%s), ('new','t',%s), ('open','t',NULL)",
      (_ago(200), _ago(10)))  # fmt: skip
    counts = retention.run_retention()
    assert (counts["turns"], counts["notifications"], counts["reports"], counts["ops_alerts"]) == (1, 1, 1, 1)
    assert {str(r["turn_id"]) for r in db._query("SELECT turn_id FROM turns")} == {TURN_IDS["new"]}
    assert {r["key"] for r in db._query("SELECT key FROM ops_alerts")} == {
        "new",
        "open",
    }  # open ones never expire


def test_audit_log_stays_append_only_but_ages_out_after_5_years(db, two_users):
    a, _ = two_users
    db._query(
        "INSERT INTO audit_log (at, actor_user_id, action) VALUES (%s,%s,'old'), (%s,%s,'recent')",
        (_ago(5 * 365 + 30), a, _ago(5 * 365 - 30), a),
        fetch=None,
        commit=True,
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db._query("UPDATE audit_log SET action='x'", fetch=None, commit=True)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db._query("DELETE FROM audit_log WHERE action='recent'", fetch=None, commit=True)
    assert retention.run_retention()["audit_log"] == 1
    assert [r["action"] for r in db._query("SELECT action FROM audit_log")] == ["recent"]


def test_report_flags_overdue_data_and_the_watchdog_pages(db, two_users, monkeypatch):
    a, _ = two_users
    _turn(db, "00000000-0000-0000-0000-0000000000cc", a, _ago(45))
    rows = {r["class"]: r for r in retention.report()}
    assert rows["turns"]["ok"] is False and rows["turns"]["age_days"] >= 44
    assert rows["notifications"]["ok"] is True  # nothing held

    on_the_hour = datetime.now(UTC).replace(minute=2)
    [firing] = watchdog._retention(on_the_hour)
    assert firing.key == "retention_overdue" and "turns" in firing.detail
    assert watchdog._retention(on_the_hour.replace(minute=30)) == []  # hourly only
    retention.run_retention()
    assert watchdog._retention(on_the_hour) == []


def test_report_script_exit_code(db, two_users, capsys, monkeypatch):
    from scripts import retention_report

    monkeypatch.setattr("config.check_boot", lambda **_: None)  # the test database is already wired in

    assert retention_report.main(["--json"]) == 0
    a, _ = two_users
    _turn(db, "00000000-0000-0000-0000-0000000000dd", a, _ago(45))
    assert retention_report.main([]) == 1
    assert retention_report.main(["--purge"]) == 0
    assert "turns" in capsys.readouterr().out


def test_run_continues_past_a_failing_class(db):
    def boom(_cutoff):
        raise RuntimeError("table locked")

    bad = retention.Rule("bad", timedelta(days=1), boom, lambda: None)
    ok = retention.Rule("ok", timedelta(days=1), lambda _c: 3, lambda: None, batch=5)
    counts = retention.run_retention(rules=[bad, ok])
    assert counts == {"bad": 0, "ok": 3}
