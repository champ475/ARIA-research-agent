"""OBS-05 on a real Postgres: non-admins are refused (403) by every admin
function and handler; admins can look up users, see turns and alerts, lock
accounts and flip flags; and every such action is in the audit log."""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest

from services import admin as svc
from utils import auth, flags

pytestmark = pytest.mark.integration


@pytest.fixture
def people(db, two_users):
    """(admin, user): two_users[0] is made an admin."""
    admin, user = two_users
    db.user_set_admin(admin, True)
    flags.clear_cache()
    return admin, user


def _audit_actions(db):
    rows = db._query("SELECT action, actor_user_id, target FROM audit_log ORDER BY id")
    return [(r["action"], r["actor_user_id"], r["target"]) for r in rows]


# ── 403 for everyone who is not an admin ─────────────────────────────────────

CALLS = {
    "find_user": lambda actor, other: svc.find_user(actor, "b@example.com"),
    "recent_turns": lambda actor, other: svc.recent_turns(actor, other),
    "alerts_of": lambda actor, other: svc.alerts_of(actor, other),
    "set_disabled": lambda actor, other: svc.set_disabled(actor, other, True),
    "grant_admin": lambda actor, other: svc.grant_admin(actor, other, True),
    "flag_list": lambda actor, other: svc.flag_list(actor),
    "set_flag": lambda actor, other: svc.set_flag(actor, "chat_enabled", False),
    "require_admin": lambda actor, other: svc.require_admin(actor),
}


def test_every_admin_function_is_covered_by_the_403_test():
    public = {n for n in svc.__all__ if inspect.isfunction(getattr(svc, n)) and n != "is_admin"}
    assert public == set(CALLS), f"add the new admin function to CALLS: {public ^ set(CALLS)}"


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_non_admin_gets_403_from_every_admin_function(db, people, name):
    admin, user = people
    for actor in (user, None, 99999):
        with pytest.raises(svc.Forbidden) as e:
            CALLS[name](actor, admin)
        assert e.value.status_code == 403 and "403" in e.value.user_message
    audited = db._query("SELECT count(*) AS n FROM audit_log WHERE action LIKE 'admin.%%'", fetch="one")["n"]
    assert audited == 0  # a refused call changes and records nothing
    assert flags.enabled("chat_enabled") is True and db.user_is_admin(admin) is True
    assert db.user_is_admin(user) is False


def test_a_disabled_admin_is_not_an_admin(db, people):
    admin, _ = people
    db.user_set_disabled(admin, True)
    with pytest.raises(svc.Forbidden):
        svc.find_user(admin, "b@example.com")


def _admin_handlers(demo):
    return [f.fn for f in demo.fns.values() if getattr(f.fn, "__module__", "") == "ui.admin"]


def test_the_ui_handlers_refuse_a_non_admin(db, people):
    """Every event handler of the Admin tab answers a non-admin with the 403 text and does nothing."""
    import app

    admin, user = people
    demo = app.build_ui()
    asking = SimpleNamespace(username=str(user))
    by_name: dict[str, list] = {}
    for fn in _admin_handlers(demo):
        by_name.setdefault(fn.__name__, []).append(fn)
    assert {"_find", "_turns", "_alerts", "_flags_view", "run"} <= set(by_name)

    def first(out):
        return str(out[0] if isinstance(out, tuple) else out)

    assert first(by_name["_find"][0](user, "a@example.com", request=asking)).startswith("⛔")
    assert first(by_name["_turns"][0](user, admin, request=asking)).startswith("⛔")
    assert first(by_name["_alerts"][0](user, admin, request=asking)).startswith("⛔")
    assert first(by_name["_flags_view"][0](user, request=asking)).startswith("⛔")
    for fn in by_name["run"]:  # disable / enable / flag on / flag off
        target = admin if "_set_disabled" in fn.__qualname__ else "chat_enabled"  # a user id, or a flag name
        out = fn(user, target, request=asking)
        assert first(out).startswith("⛔") and "403" in first(out), fn.__qualname__
    assert db.user_is_admin(user) is False
    assert (
        db._query("SELECT disabled_at FROM users WHERE id=%s", (admin,), fetch="one")["disabled_at"] is None
    )
    assert flags.enabled("chat_enabled") is True


# ── what an admin can do ─────────────────────────────────────────────────────


def test_lookup_shows_support_facts_and_no_secrets(db, people):
    admin, user = people
    db.telegram_chat_id_set(user, "12345")
    db.alert_add(user, "TCS", "price_above", "c", 4000.0)
    view = svc.find_user(admin, "  B@Example.com ")
    assert view["id"] == user and view["telegram_linked"] is True and view["byo"] is False
    assert view["active_alerts"] == 1 and view["quota"].endswith("free messages today")
    assert view["verified"] is False and view["disabled"] is False
    flat = " ".join(map(str, view.values()))
    assert "12345" not in flat and "password" not in flat.lower()
    assert svc.find_user(admin, "nobody@example.com") is None
    assert ("admin.lookup", admin, str(user)) in _audit_actions(db)


def test_turns_and_alerts_answer_the_support_questions(db, people):
    admin, user = people
    db._query(
        "INSERT INTO turns (turn_id, user_id, created_at, status, error_id, intent, latency_ms, llm_calls, "
        "tokens_in, tokens_out, cost_inr) VALUES ('00000000-0000-0000-0000-0000000000a1', %s, now(), 'ok', "
        "'abc12345', 'equity', 4200, 4, 1000, 500, 0.42)",
        (user,),
        fetch=None,
        commit=True,
    )
    [turn] = svc.recent_turns(admin, user)
    assert turn["error_id"] == "abc12345" and turn["tokens"] == 1500 and turn["cost_inr"] == "0.420"
    assert turn["latency_s"] == "4.2" and "find_turn.py abc12345" in turn["trace"]
    assert svc.recent_turns(admin, None)[0]["error_id"] == "abc12345"  # everyone's

    aid = db.alert_add(user, "TCS", "price_above", "TCS above 4000", 4000.0)
    db._query(
        "UPDATE alerts SET last_checked_at=now(), delivery_status='pending_delivery', delivery_attempts=3, "
        "last_delivery_error='chat not found' WHERE id=%s",
        (aid,),
        fetch=None,
        commit=True,
    )
    [a] = svc.alerts_of(admin, user)
    assert a["status"] == "Active" and a["last_checked"] != "—" and a["delivery"] == "Waiting in your inbox"
    assert a["attempts"] == 3 and a["last_error"] == "chat not found"
    actions = _audit_actions(db)
    assert ("admin.turns", admin, str(user)) in actions and ("admin.alerts", admin, str(user)) in actions


def test_disabling_ends_sessions_and_blocks_login_until_enabled(db, people):
    admin, user = people
    pw = "correct-horse-battery-staple"
    db._query(
        "UPDATE users SET password_hash=%s WHERE id=%s",
        (auth._hash_password(pw), user),
        fetch=None,
        commit=True,
    )
    token = auth.login("b@example.com", pw)
    assert auth.session_info(token) is not None

    assert svc.set_disabled(admin, user, True) is True
    auth.clear_session_cache()
    assert auth.session_info(token) is None  # the live session is gone
    with pytest.raises(auth.AuthError, match="disabled"):
        auth.login("b@example.com", pw)
    assert svc.find_user(admin, "b@example.com")["disabled"] is True

    assert svc.set_disabled(admin, user, False) is True
    assert auth.session_info(auth.login("b@example.com", pw)) is not None
    actions = _audit_actions(db)
    assert ("admin.user_disable", admin, str(user)) in actions
    assert ("admin.user_enable", admin, str(user)) in actions


def test_a_wrong_password_on_a_disabled_account_reveals_nothing(db, people):
    admin, user = people
    svc.set_disabled(admin, user, True)
    with pytest.raises(auth.AuthError, match="Invalid email or password"):
        auth.login("b@example.com", "not-the-password-1")


def test_an_admin_cannot_lock_or_demote_themselves(db, people):
    admin, _ = people
    with pytest.raises(svc.Forbidden):
        svc.set_disabled(admin, admin, True)
    with pytest.raises(svc.Forbidden):
        svc.grant_admin(admin, admin, False)
    assert db.user_is_admin(admin) is True


def test_grant_and_revoke(db, people):
    admin, user = people
    assert svc.grant_admin(admin, user, True) is True and svc.find_user(user, "a@example.com")
    assert svc.grant_admin(user, admin, False) is True and db.user_is_admin(admin) is False
    actions = _audit_actions(db)
    assert ("admin.grant", admin, str(user)) in actions and ("admin.revoke", user, str(admin)) in actions


def test_flags_default_on_change_and_are_audited(db, people):
    admin, _ = people
    assert [f["enabled"] for f in svc.flag_list(admin)] == [True, True]
    svc.set_flag(admin, "signups_open", False)
    assert flags.enabled("signups_open") is False and flags.enabled("chat_enabled") is True
    assert db.flags_all() == {"signups_open": False}
    with pytest.raises(KeyError):
        svc.set_flag(admin, "no_such_flag", True)
    assert ("admin.flag_set", admin, "signups_open") in _audit_actions(db)
    svc.set_flag(admin, "signups_open", True)
    assert flags.enabled("signups_open") is True


def test_flags_fall_back_to_defaults_when_the_database_cannot_be_read(monkeypatch):
    flags.clear_cache()
    monkeypatch.setattr("utils.db.flags_all", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert flags.enabled("signups_open") is True and flags.enabled("chat_enabled") is True
    flags.clear_cache()


def test_signups_closed_flag_refuses_new_accounts(db, people):
    import gradio as gr
    from fastapi.testclient import TestClient

    from server import create_app

    admin, _ = people
    svc.set_flag(admin, "signups_open", False)
    with gr.Blocks() as demo:
        gr.Textbox()
    with TestClient(create_app(demo)) as client:
        html = client.get("/signup").text
        token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        r = client.post(
            "/signup",
            data={
                "email": "new@example.com",
                "password": "another-long-pass-9",
                "agree": "yes",
                "csrf": token,
            },
        )
    assert r.status_code == 403 and "Sign-up is closed" in r.text
    assert db._query("SELECT count(*) AS n FROM users WHERE email='new@example.com'", fetch="one")["n"] == 0


def test_chat_switched_off_gives_a_maintenance_message(db, people):
    from ui import chat as chat_ui

    admin, _ = people
    svc.set_flag(admin, "chat_enabled", False)
    (only,) = list(chat_ui.respond_exclusive(admin, "TCS price", [], [], 1, "1M"))
    assert "paused for maintenance" in only[2][-1]["content"]
