"""SEC-04 on a real Postgres: cookie sessions that survive a refresh, expire,
can be logged out and revoked, are checked on every Gradio request, and are
stored only as hashes."""

from __future__ import annotations

import hashlib
import re

import gradio as gr
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"


@pytest.fixture
def client(db):
    from server import create_app
    from utils import auth

    auth.clear_session_cache()
    with gr.Blocks() as demo:
        gr.Textbox(label="probe")
    with TestClient(create_app(demo), base_url="http://testserver") as c:
        yield c
    auth.clear_session_cache()


def _csrf(client: TestClient, page: str) -> str:
    r = client.get(page)
    assert r.status_code == 200
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m
    return m.group(1)


def _signup(client: TestClient, email: str = "alice@example.com") -> str:
    token = _csrf(client, "/signup")
    r = client.post(
        "/signup",
        data={"email": email, "password": PASSWORD, "agree": "yes", "csrf": token},
        follow_redirects=False,
    )
    # SEC-05: signup never starts a session (same page for new and existing
    # addresses); the user logs in. Verification is off in this env.
    assert r.status_code == 200 and "Check your email" in r.text
    assert "aria_session" not in client.cookies
    token = _csrf(client, "/login")
    r = client.post(
        "/login", data={"email": email, "password": PASSWORD, "csrf": token}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "aria_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    return client.cookies.get("aria_session")


def test_signup_sets_an_httponly_cookie_and_the_db_stores_only_its_hash(client, db):
    token = _signup(client)
    rows = db._query("SELECT * FROM sessions")
    assert len(rows) == 1
    assert rows[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert all(token not in str(v) for v in rows[0].values())
    assert db._query("SELECT accepted_terms_at FROM users", fetch="one")["accepted_terms_at"] is not None


def test_refresh_keeps_you_logged_in_and_gradio_routes_need_the_cookie(client):
    _signup(client)
    page = client.get("/", follow_redirects=False)  # a "refresh"
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"  # back button after logout re-requests
    assert client.get("/config").status_code == 200
    anon = TestClient(client.app, base_url="http://testserver")
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert anon.get("/config").status_code == 401


def test_logout_makes_the_token_unusable(client):
    from utils.auth import resolve_session

    token = _signup(client)
    r = client.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert resolve_session(token) is None
    client.cookies.set("aria_session", token)  # replay the old cookie
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/config").status_code == 401


def test_expired_session_gets_no_access(client, db):
    from utils import auth

    token = _signup(client)
    db._query("UPDATE sessions SET expires_at = now() - interval '1 minute'", fetch=None, commit=True)
    auth.clear_session_cache()
    assert auth.resolve_session(token) is None
    assert client.get("/config").status_code == 401
    assert db.auth_sessions_purge_expired() == 1
    assert db._query("SELECT count(*) AS n FROM sessions", fetch="one")["n"] == 0


def test_login_rotates_tokens_and_logout_other_devices(client, db):
    from utils import auth

    first = _signup(client)
    other = TestClient(client.app, base_url="http://testserver")
    csrf = _csrf(other, "/login")
    r = other.post(
        "/login",
        data={"email": "alice@example.com", "password": PASSWORD, "csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    second = other.cookies.get("aria_session")
    assert second and second != first  # a fresh token per login
    uid = auth.resolve_session(first)
    assert auth.resolve_session(second) == uid
    assert len(db.auth_sessions_list(uid)) == 2

    assert auth.logout_other_devices(uid, first) == 1
    assert auth.resolve_session(second) is None
    assert auth.resolve_session(first) == uid


def test_wrong_password_and_missing_csrf_are_refused(client):
    _signup(client)
    client.cookies.clear()
    csrf = _csrf(client, "/login")
    r = client.post("/login", data={"email": "alice@example.com", "password": "nope-nope", "csrf": csrf})
    assert r.status_code == 400 and "Invalid email or password." in r.text
    assert "aria_session" not in client.cookies
    r = client.post("/login", data={"email": "alice@example.com", "password": PASSWORD, "csrf": "forged"})
    assert r.status_code == 400 and "expired" in r.text


def test_signup_requires_consent(client):
    csrf = _csrf(client, "/signup")
    r = client.post("/signup", data={"email": "bob@example.com", "password": PASSWORD, "csrf": csrf})
    assert r.status_code == 400 and "agree" in r.text


def test_terms_page_renders_sanitised_legal_text(client):
    r = client.get("/terms")
    assert r.status_code == 200 and "<script" not in r.text.lower()
