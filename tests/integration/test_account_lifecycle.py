"""SEC-06 on a real Postgres: email verification gates the app, password
reset with expired / reused / wrong tokens, and self-serve deletion that
leaves no row for the user in any table."""

from __future__ import annotations

import re

import gradio as gr
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

PASSWORD = "first-password-1"


@pytest.fixture
def client(db, monkeypatch):
    import config
    from server import create_app
    from utils import auth, mailer

    monkeypatch.setattr(config, "REQUIRE_EMAIL_VERIFICATION", True)
    monkeypatch.setattr(config, "MAIL_BACKEND", "console")
    mailer.outbox.clear()
    auth.clear_session_cache()
    with gr.Blocks() as demo:
        gr.Textbox()
    with TestClient(create_app(demo)) as c:
        yield c
    auth.clear_session_cache()


def _form(client, page, post_to=None, **data):
    html = client.get(page).text
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    return client.post(post_to or page, data={**data, "csrf": csrf}, follow_redirects=False)


def _last_link(kind: str) -> str:
    from utils import mailer

    m = re.search(rf"(/auth/{kind}/\S+)", mailer.outbox[-1].text)
    assert m, mailer.outbox[-1].text
    return m.group(1)


def _signup(client, email="dana@example.com"):
    r = _form(client, "/signup", email=email, password=PASSWORD, agree="yes")
    assert r.status_code == 303


def test_unverified_account_is_held_at_the_verify_page_until_the_link_is_used(client):
    from utils import mailer

    _signup(client)
    assert mailer.outbox[-1].to == "dana@example.com"
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/verify-pending"
    assert client.get("/config").status_code == 401  # no chat before verification
    assert "dana@example.com" in client.get("/auth/verify-pending").text

    link = _last_link("verify")
    r = client.get(link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get(link).status_code == 400  # single use


def test_expired_and_wrong_verification_tokens_are_refused(client, db):
    _signup(client)
    link = _last_link("verify")
    db._query("UPDATE auth_tokens SET expires_at = now() - interval '1 minute'", fetch=None, commit=True)
    assert client.get(link).status_code == 400
    assert client.get("/auth/verify/not-a-real-token").status_code == 400


def test_resend_issues_a_new_link_and_invalidates_the_old_one(client):
    from utils import mailer

    _signup(client)
    first = _last_link("verify")
    r = _form(client, "/auth/verify-pending", post_to="/auth/verify/resend")
    assert r.status_code == 303
    second = _last_link("verify")
    assert second != first
    assert client.get(first).status_code == 400
    sent = len(mailer.outbox)
    for _ in range(5):  # rate-limited: at most 3 resends an hour (one already used)
        _form(client, "/auth/verify-pending", post_to="/auth/verify/resend")
    assert len(mailer.outbox) - sent == 2
    assert client.get(_last_link("verify"), follow_redirects=False).status_code == 303


def test_forgot_password_answers_the_same_for_unknown_addresses(client):
    from utils import mailer

    _signup(client)
    client.cookies.clear()
    before = len(mailer.outbox)
    known = _form(client, "/auth/forgot", email="dana@example.com")
    unknown = _form(client, "/auth/forgot", email="nobody@example.com")
    assert known.status_code == unknown.status_code == 200
    assert "If an account exists" in known.text and "If an account exists" in unknown.text
    assert len(mailer.outbox) == before + 1 and mailer.outbox[-1].to == "dana@example.com"


def test_reset_with_expired_reused_and_wrong_tokens(client, db):
    from utils import auth

    _signup(client)
    old_cookie = client.cookies.get("aria_session")
    _form(client, "/auth/forgot", email="dana@example.com")
    link = _last_link("reset")

    bad = _form(client, link, password="new-password-22", confirm="new-password-22x")
    assert bad.status_code == 400 and "match" in bad.text
    wrong = _form(client, "/auth/reset/wrong-token", password="new-password-22", confirm="new-password-22")
    assert wrong.status_code == 400 and "invalid" in wrong.text

    ok = _form(client, link, password="new-password-22", confirm="new-password-22")
    assert ok.status_code == 303 and ok.headers["location"] == "/login?n=reset"
    assert auth.resolve_session(old_cookie) is None  # every session revoked
    reused = _form(client, link, password="another-pass-33", confirm="another-pass-33")
    assert reused.status_code == 400

    _form(client, "/auth/forgot", email="dana@example.com")
    expired_link = _last_link("reset")
    db._query(
        "UPDATE auth_tokens SET expires_at = now() - interval '1 minute' WHERE used_at IS NULL",
        fetch=None,
        commit=True,
    )
    assert _form(client, expired_link, password="third-pass-44", confirm="third-pass-44").status_code == 400

    client.cookies.clear()
    login = _form(client, "/login", email="dana@example.com", password="new-password-22")
    assert login.status_code == 303


def test_password_length_is_bounded_before_hashing(client):
    r = _form(client, "/signup", email="long@example.com", password="x" * 129, agree="yes")
    assert r.status_code == 400 and "at most 128" in r.text


def test_delete_account_leaves_zero_rows_for_the_user_in_every_table(client, db, two_users):
    from utils import auth

    a, b = two_users  # b is a bystander whose rows must survive
    _signup(client, "erin@example.com")
    uid = db.user_by_email("erin@example.com")["id"]
    db.user_mark_verified(uid)
    auth.clear_session_cache()
    db.watchlist_add(uid, "TCS")
    db.alert_add(uid, "TCS", "price_below", "below 3500", 3500.0)
    sid = db.session_create(uid, "chat", title="mine")
    db.chat_save(uid, sid, "chat", "user", "hello")
    db.profile_upsert(uid, {"age": 30})
    db.llm_config_set(uid, "openai_compatible", "m", "enc")
    db.watchlist_add(b, "INFY")

    wrong = _form(client, "/account/delete", password="nope", confirm="DELETE")
    assert wrong.status_code == 400 and "not correct" in wrong.text
    unconfirmed = _form(client, "/account/delete", password=PASSWORD, confirm="")
    assert unconfirmed.status_code == 400
    r = _form(client, "/account/delete", password=PASSWORD, confirm="DELETE")
    assert r.status_code == 303 and r.headers["location"] == "/login?n=deleted"

    tables = db._query(
        "SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='user_id'"
    )
    assert len(tables) >= 13
    for t in tables:
        n = db._query(f'SELECT count(*) AS n FROM "{t["table_name"]}" WHERE user_id=%s', (uid,), fetch="one")[
            "n"
        ]  # noqa: S608
        assert n == 0, t["table_name"]
    assert db._query("SELECT count(*) AS n FROM users WHERE id=%s", (uid,), fetch="one")["n"] == 0
    assert db.watchlist_all(b) == ["INFY"]
    assert db.audit_log_for(uid)[0]["action"] == "account.delete"  # the trail survives
    assert client.get("/config").status_code == 401
