"""COMP-02 on a real Postgres: versioned consent and forced re-consent after
a material change."""

from __future__ import annotations

import re

import gradio as gr
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

PASSWORD = "consent-pass-1"


@pytest.fixture
def client(db):
    from server import create_app
    from utils import auth

    auth.clear_session_cache()
    with gr.Blocks() as demo:
        gr.Textbox()
    with TestClient(create_app(demo)) as c:
        yield c
    auth.clear_session_cache()
    db._query("DELETE FROM terms_versions WHERE version LIKE '2099-%%'", fetch=None, commit=True)


def _form(client, page, post_to=None, **data):
    html = client.get(page).text
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    return client.post(post_to or page, data={**data, "csrf": csrf}, follow_redirects=False)


def _signup_and_login(client, email="ivy@example.com"):
    _form(client, "/signup", email=email, password=PASSWORD, agree="yes")
    assert _form(client, "/login", email=email, password=PASSWORD).status_code == 303


def _publish(db, version: str, material: bool, effective: str = "now()") -> None:
    db._query(
        f"INSERT INTO terms_versions (version, effective_at, material, summary) VALUES (%s, {effective}, %s, 'test')",  # noqa: S608
        (version, material),
        fetch=None,
        commit=True,
    )


def test_legal_md_version_is_published(db):
    """Editing LEGAL.md's version without a migration row would lock users out."""
    from utils.terms import current_version

    assert db.terms_version_get(current_version()) is not None


def test_signup_records_the_accepted_version(client, db):
    from utils.terms import current_version

    _signup_and_login(client)
    row = db._query("SELECT accepted_terms_version, accepted_terms_at FROM users", fetch="one")
    assert row["accepted_terms_version"] == current_version() and row["accepted_terms_at"] is not None
    assert client.get("/", follow_redirects=False).status_code == 200


def test_material_change_forces_re_consent(client, db, monkeypatch):
    from utils import auth, terms

    _signup_and_login(client)
    _publish(db, "2099-01-01", material=True)
    auth.clear_session_cache()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/terms/accept"
    assert client.get("/config").status_code == 401  # blocked until they accept

    monkeypatch.setattr(terms, "current_version", lambda: "2099-01-01")
    refused = _form(client, "/terms/accept")  # checkbox not ticked
    assert refused.headers["location"] == "/terms/accept"
    ok = _form(client, "/terms/accept", agree="yes")
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert client.get("/", follow_redirects=False).status_code == 200
    assert (
        db._query("SELECT accepted_terms_version FROM users", fetch="one")["accepted_terms_version"]
        == "2099-01-01"
    )
    actions = [r["action"] for r in db._query("SELECT action FROM audit_log ORDER BY id")]
    assert actions.count("auth.terms_accepted") == 2


def test_minor_and_future_changes_do_not_interrupt(client, db):
    from utils import auth

    _signup_and_login(client)
    _publish(db, "2099-02-01", material=False)
    _publish(db, "2099-03-01", material=True, effective="now() + interval '7 days'")
    auth.clear_session_cache()
    assert client.get("/", follow_redirects=False).status_code == 200


def test_users_who_never_accepted_a_version_must_accept(client, db):
    from utils import auth

    _signup_and_login(client)
    db._query("UPDATE users SET accepted_terms_version = NULL", fetch=None, commit=True)
    auth.clear_session_cache()
    assert client.get("/", follow_redirects=False).headers["location"] == "/terms/accept"
