"""
utils/auth.py — signup/login/session helpers.

Password hashing: PBKDF2-HMAC-SHA256, stdlib only (no new dependency).
Sessions: opaque random token, stored server-side in Postgres `sessions`
table, checked on every request. This is the Phase-1 foundation piece of
docs/PRD.md §7.1/§7.2 — email verification, OAuth, MFA are fast-follows,
not implemented here.
"""

import hashlib
import os
import re
import secrets
from datetime import UTC, datetime, timedelta

from utils.db import auth_attempt_clear, auth_attempt_count, auth_attempt_record, get_conn, release_conn
from utils.errors import UserInputError

SESSION_TTL_HOURS = 24 * 14  # 14 days
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_PBKDF2_ITERATIONS = 260_000

# Postgres-backed sliding-window throttle (auth_attempts table), keyed by
# email — durable across restarts, unlike an in-memory dict, and correct
# for a multi-instance deployment too (all instances share the same table).
_RATE_LIMIT_WINDOW_S = 5 * 60
_RATE_LIMIT_MAX_ATTEMPTS = 5


def _check_rate_limit(email: str):
    """Raises AuthError if `email` has hit the attempt cap within the window.
    Call BEFORE the main DB work so a flood doesn't also hammer the rest of
    signup/login — this is still one query against a small, indexed table,
    not free, but far cheaper than a password hash + user lookup."""
    since = datetime.now(UTC) - timedelta(seconds=_RATE_LIMIT_WINDOW_S)
    if auth_attempt_count(email, since) >= _RATE_LIMIT_MAX_ATTEMPTS:
        raise AuthError("Too many attempts. Please wait a few minutes and try again.")
    auth_attempt_record(email)


def _clear_rate_limit(email: str):
    auth_attempt_clear(email)


class AuthError(UserInputError):
    """Raised for signup/login failures with a user-facing message."""


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, _ = stored.split("$", 1)
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    return secrets.compare_digest(_hash_password(password, salt), stored)


def signup(email: str, password: str) -> int:
    """Create a new user. Returns the new user_id. Raises AuthError on bad input."""
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address.")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    _check_rate_limit(f"signup:{email}")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email=%s", (email,))
        if cur.fetchone():
            raise AuthError("An account with this email already exists.")

        password_hash = _hash_password(password)
        cur.execute(
            "INSERT INTO users (email, password_hash) VALUES (%s,%s) RETURNING id",
            (email, password_hash),
        )
        user_id = cur.fetchone()["id"]
        conn.commit()
        return user_id
    finally:
        release_conn(conn)


def login(email: str, password: str) -> str:
    """Verify credentials and return a new session token. Raises AuthError on failure."""
    email = email.strip().lower()
    _check_rate_limit(f"login:{email}")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, password_hash FROM users WHERE email=%s", (email,))
        row = cur.fetchone()
        if not row or not _verify_password(password, row["password_hash"]):
            raise AuthError("Invalid email or password.")

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS)
        cur.execute(
            "INSERT INTO sessions (user_id, token, expires_at) VALUES (%s,%s,%s)",
            (row["id"], token, expires_at),
        )
        conn.commit()
        _clear_rate_limit(f"login:{email}")
        return token
    finally:
        release_conn(conn)


def logout(token: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
        conn.commit()
    finally:
        release_conn(conn)


def get_user_id(token: str) -> int | None:
    """Resolve a session token to a user_id, or None if invalid/expired."""
    if not token:
        return None
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=%s", (token,))
        row = cur.fetchone()
    finally:
        release_conn(conn)
    if not row:
        return None
    if row["expires_at"] < datetime.now(UTC):
        return None
    return row["user_id"]
