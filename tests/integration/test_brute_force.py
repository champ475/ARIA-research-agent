"""SEC-05 on a real Postgres: per-IP limits across emails, per-account
exponential backoff, no account enumeration at signup, optional CAPTCHA,
and a bounded auth_attempts table."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import gradio as gr
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture
def client(db):
    from server import create_app
    from utils import auth, mailer

    mailer.outbox.clear()
    auth.clear_session_cache()
    with gr.Blocks() as demo:
        gr.Textbox()
    with TestClient(create_app(demo)) as c:
        yield c


def _form(client, page, **data):
    html = client.get(page).text
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    return client.post(page, data={**data, "csrf": csrf}, follow_redirects=False)


def test_21st_attempt_from_one_ip_across_21_emails_is_blocked(client):
    replies = [
        _form(client, "/login", email=f"user{i}@example.com", password="wrong-pass-1").text for i in range(21)
    ]
    assert all("Invalid email or password." in r for r in replies[:20])
    assert "Too many attempts" in replies[20]


def test_signup_with_an_existing_email_looks_exactly_like_a_new_one(client, db):
    from utils import mailer

    new = _form(client, "/signup", email="frank@example.com", password="pass-word-123", agree="yes")
    again = _form(client, "/signup", email="frank@example.com", password="other-pass-456", agree="yes")

    def normalise(html: str) -> str:
        return re.sub(r'value="[^"]+"', 'value=""', html)

    assert new.status_code == again.status_code == 200
    assert normalise(new.text) == normalise(again.text)
    assert "aria_session" not in client.cookies
    assert db._query("SELECT count(*) AS n FROM users", fetch="one")["n"] == 1
    subjects = [m.subject for m in mailer.outbox if m.to == "frank@example.com"]
    assert subjects == ["Confirm your ARIA email address", "You already have an ARIA account"]


def test_per_account_backoff_after_many_failures(db):
    from utils import auth

    auth.signup("gina@example.com", "right-password-1")
    old = datetime.now(UTC) - timedelta(minutes=10)
    for _ in range(10):  # 10 failures, the last 10 minutes ago -> 32 min backoff
        db._query(
            "INSERT INTO auth_attempts (key, attempted_at) VALUES (%s,%s)",
            ("login:gina@example.com", old),
            fetch=None,
            commit=True,
        )
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.authenticate("gina@example.com", "right-password-1")
    db._query("DELETE FROM auth_attempts", fetch=None, commit=True)
    for _ in range(6):  # 6 failures -> 2 min backoff, already elapsed
        db._query(
            "INSERT INTO auth_attempts (key, attempted_at) VALUES (%s,%s)",
            ("login:gina@example.com", old),
            fetch=None,
            commit=True,
        )
    assert auth.authenticate("gina@example.com", "right-password-1") > 0


def test_backoff_schedule():
    from utils.auth import _backoff_seconds

    assert [_backoff_seconds(n) for n in (0, 4, 5, 6, 7, 20)] == [0, 0, 60, 120, 240, 3600]


def test_captcha_is_required_after_three_failures_when_configured(client, monkeypatch):
    import config
    from web import captcha

    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "site-key")
    monkeypatch.setattr(config, "TURNSTILE_SECRET", "secret")
    from utils import auth

    auth.signup("hana@example.com", "right-password-1")
    assert "cf-turnstile" not in client.get("/login").text
    for _ in range(3):
        _form(client, "/login", email="hana@example.com", password="wrong-pass-9")
    assert "cf-turnstile" in client.get("/login").text
    blocked = _form(client, "/login", email="hana@example.com", password="right-password-1")
    assert blocked.status_code == 400 and "complete the check" in blocked.text
    monkeypatch.setattr(captcha, "verify", lambda token, ip: True)
    ok = _form(client, "/login", email="hana@example.com", password="right-password-1")
    assert ok.status_code == 303


def test_housekeeping_keeps_auth_attempts_bounded(db):
    from utils.maintenance import run_housekeeping

    two_days = datetime.now(UTC) - timedelta(days=2)
    for i in range(5):
        db._query(
            "INSERT INTO auth_attempts (key, attempted_at) VALUES (%s,%s)",
            (f"ip:old{i}", two_days),
            fetch=None,
            commit=True,
        )
    db.auth_attempt_record("ip:recent")
    counts = run_housekeeping()
    assert counts["auth_attempts"] == 5
    assert db._query("SELECT count(*) AS n FROM auth_attempts", fetch="one")["n"] == 1
