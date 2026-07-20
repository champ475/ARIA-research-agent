"""
utils/auth.py — signup/login/session helpers.

Password hashing: PBKDF2-HMAC-SHA256, stdlib only (no new dependency).
Sessions: opaque random token, stored server-side in SQLite `sessions`
table, checked on every request. This is the Phase-1 foundation piece of
docs/PRD.md §7.1/§7.2 — email verification, OAuth, MFA are fast-follows,
not implemented here.
"""

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta

from utils.db import get_conn
from utils.helpers import now_ist

SESSION_TTL_HOURS = 24 * 14  # 14 days
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_PBKDF2_ITERATIONS = 260_000


class AuthError(Exception):
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

    conn = get_conn()
    existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        conn.close()
        raise AuthError("An account with this email already exists.")

    password_hash = _hash_password(password)
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
        (email, password_hash, now_ist()),
    )
    user_id = cur.lastrowid
    conn.commit()
    conn.close()
    return user_id


def login(email: str, password: str) -> str:
    """Verify credentials and return a new session token. Raises AuthError on failure."""
    email = email.strip().lower()
    conn = get_conn()
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE email=?", (email,)
    ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        conn.close()
        raise AuthError("Invalid email or password.")

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
    conn.execute(
        "INSERT INTO sessions (user_id, token, expires_at) VALUES (?,?,?)",
        (row["id"], token, expires_at),
    )
    conn.commit()
    conn.close()
    return token


def logout(token: str):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


def get_user_id(token: str) -> int | None:
    """Resolve a session token to a user_id, or None if invalid/expired."""
    if not token:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT user_id, expires_at FROM sessions WHERE token=?", (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
        return None
    return row["user_id"]
