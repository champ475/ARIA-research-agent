"""COMP-04 on a real Postgres: an audit row per auditable event, and the
table can't be modified or deleted from."""

from __future__ import annotations

import re
from pathlib import Path

import gradio as gr
import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.integration.test_tenant_isolation import _run
from utils.audit import ACTIONS

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _actions(db) -> list[str]:
    return [r["action"] for r in db._query("SELECT action FROM audit_log ORDER BY id")]


@pytest.fixture
def client(db):
    from server import create_app
    from utils import auth

    auth.clear_session_cache()
    with gr.Blocks() as demo:
        gr.Textbox()
    with TestClient(create_app(demo)) as c:
        yield c


def _form(client, page, **data):
    html = client.get(page).text
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    return client.post(page, data={**data, "csrf": csrf}, follow_redirects=False)


def test_auth_events_are_audited(client, db):
    _form(client, "/signup", email="carol@example.com", password="long-password-1", agree="yes")
    client.post("/auth/logout", follow_redirects=False)
    _form(client, "/login", email="carol@example.com", password="wrong-password")
    _form(client, "/login", email="carol@example.com", password="long-password-1")
    assert _actions(db) == [
        "auth.signup",
        "auth.terms_accepted",
        "auth.logout",
        "auth.login_failed",
        "auth.login",
    ]
    failed = db._query("SELECT * FROM audit_log WHERE action='auth.login_failed'", fetch="one")
    assert failed["actor_user_id"] is None and failed["target"].startswith("email-sha256:")
    assert "carol" not in str(failed)  # the typed address isn't stored
    ok = db._query("SELECT * FROM audit_log WHERE action='auth.login'", fetch="one")
    assert ok["actor_user_id"] is not None and ok["ip"]


def test_alert_and_screener_writes_are_audited(db, two_users):
    a, _ = two_users
    created = _run("alert_create", "TCS", a, "alert me if TCS drops below 3500")
    _run("alert_pause", "TCS", a, "pause my TCS alert")
    _run("alert_resume", "TCS", a, "resume my TCS alert")
    _run("alert_cancel", "TCS", a, "cancel my TCS alert")
    _run(
        "screener_save",
        "market",
        a,
        "save this screen as cheap",
        intent="screener",
        extra={"screener_parse_query": {"pe_max": 20}},
    )
    rows = db._query("SELECT action, actor_user_id, target FROM audit_log ORDER BY id")
    assert [r["action"] for r in rows] == [
        "alert.create",
        "alert.pause",
        "alert.resume",
        "alert.cancel",
        "screener.save",
    ]
    assert all(r["actor_user_id"] == a for r in rows)
    assert rows[0]["target"] == f"alert:{created['alert_id']}"
    assert db.audit_log_for(a)[0]["action"] == "screener.save"


def test_audit_rows_cannot_be_changed_or_deleted(db):
    from utils.audit import audit

    audit("auth.login", actor_user_id=1, details={"note": "x", "api_key": "sk-abcdefghijklmnop"})
    row = db._query("SELECT details FROM audit_log", fetch="one")
    assert "abcdefghijklmnop" not in str(row["details"])  # secrets redacted as a backstop
    for sql in ("UPDATE audit_log SET action='tampered'", "DELETE FROM audit_log"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db._query(sql, fetch=None, commit=True)
    assert _actions(db) == ["auth.login"]


def test_public_cannot_update_or_delete(db):
    rows = db._query(
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE table_name='audit_log' AND grantee='PUBLIC'"
    )
    assert not {r["privilege_type"] for r in rows} & {"UPDATE", "DELETE"}


def test_every_audit_action_has_a_call_site():
    """Each declared event is actually recorded somewhere (COMP-04's list),
    except the ones whose features arrive later in Phase 2."""
    pending = {"auth.password_reset", "auth.email_verified", "account.export", "account.delete"}
    code = "\n".join(
        p.read_text(encoding="utf-8")
        for d in ("app.py", "web", "graph", "utils", "agents")
        for p in ([ROOT / d] if d.endswith(".py") else (ROOT / d).rglob("*.py"))
        if p.name != "audit.py"
    )
    missing = sorted(a for a in ACTIONS - pending if f'"{a}"' not in code and "alert." not in a)
    assert missing == []
