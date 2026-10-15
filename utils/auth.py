"""
utils/auth.py — signup, login and cookie sessions.

Password hashing: PBKDF2-HMAC-SHA256, stdlib only.

Sessions (SEC-04): a random token lives only in the user's HttpOnly cookie;
Postgres `sessions` stores sha256(token), its expiry, last use and client.
`resolve_session()` is called for every Gradio request (web/session.py's
auth_dependency) — an expired, logged-out or revoked session stops working
on the next request (within SESSION_CACHE_SECONDS on other replicas).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta

from cachetools import TTLCache

from utils import db
from utils.db import auth_attempt_clear, auth_attempt_count, auth_attempt_record, get_conn, release_conn
from utils.errors import UserInputError

SESSION_TTL_HOURS = 24 * 14  # 14 days
# A resolved session is trusted for this long without a DB round trip. Logout
# on this replica evicts immediately; other replicas notice within this window.
SESSION_CACHE_SECONDS = 30
# last_seen_at is written at most this often per session.
_TOUCH_INTERVAL_SECONDS = 300

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_PBKDF2_ITERATIONS = 260_000

# Postgres-backed sliding-window throttle (auth_attempts table), keyed by
# email — durable across restarts and shared by every instance.
_RATE_LIMIT_WINDOW_S = 5 * 60
_RATE_LIMIT_MAX_ATTEMPTS = 5

_cache: TTLCache[str, int] = TTLCache(maxsize=10_000, ttl=SESSION_CACHE_SECONDS)
_last_touch: TTLCache[str, float] = TTLCache(maxsize=10_000, ttl=_TOUCH_INTERVAL_SECONDS * 2)
_cache_lock = threading.Lock()


def _check_rate_limit(email: str):
    """Raises AuthError if `email` has hit the attempt cap within the window.
    Call BEFORE the main DB work so a flood doesn't also hammer the rest of
    signup/login."""
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


def hash_token(token: str) -> str:
    """What the database stores for a session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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


def authenticate(email: str, password: str) -> int:
    """Check credentials; returns the user id or raises AuthError."""
    email = email.strip().lower()
    _check_rate_limit(f"login:{email}")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, password_hash FROM users WHERE email=%s", (email,))
        row = cur.fetchone()
    finally:
        release_conn(conn)
    if not row or not _verify_password(password, row["password_hash"]):
        raise AuthError("Invalid email or password.")
    _clear_rate_limit(f"login:{email}")
    return row["id"]


def create_session(user_id: int, ip: str | None = None, user_agent: str | None = None) -> str:
    """Start a new login session; returns the token for the cookie. Every login
    gets a fresh token (rotation), so a token seen before login is useless."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS)
    db.auth_session_create(user_id, hash_token(token), expires_at, ip=ip, user_agent=user_agent)
    return token


def login(email: str, password: str, ip: str | None = None, user_agent: str | None = None) -> str:
    """Verify credentials and return a new session token. Raises AuthError on failure."""
    return create_session(authenticate(email, password), ip=ip, user_agent=user_agent)


def resolve_session(token: str | None) -> int | None:
    """The user id behind a session cookie, or None if missing, unknown,
    expired or logged out."""
    if not token:
        return None
    th = hash_token(token)
    with _cache_lock:
        cached = _cache.get(th)
    if cached is not None:
        return cached
    row = db.auth_session_lookup(th)
    if row is None:
        return None
    user_id = int(row["user_id"])
    with _cache_lock:
        _cache[th] = user_id
        touch = th not in _last_touch
        if touch:
            _last_touch[th] = time.monotonic()
    if touch:
        db.auth_session_touch(th)
    return user_id


def get_user_id(token: str) -> int | None:
    """Backwards-compatible alias of resolve_session()."""
    return resolve_session(token)


def _forget(token_hash: str) -> None:
    with _cache_lock:
        _cache.pop(token_hash, None)
        _last_touch.pop(token_hash, None)


def logout(token: str | None) -> None:
    """End this session; its cookie stops working immediately."""
    if not token:
        return
    th = hash_token(token)
    _forget(th)
    db.auth_session_delete(th)


def logout_other_devices(user_id: int, current_token: str | None) -> int:
    """Revoke every session of `user_id` except the current one."""
    keep = hash_token(current_token) if current_token else None
    n = db.auth_sessions_revoke_others(user_id, keep)
    with _cache_lock:
        for th in [k for k, uid in _cache.items() if uid == user_id and k != keep]:
            _cache.pop(th, None)
    return n


def clear_session_cache() -> None:
    """Tests / after bulk revocation."""
    with _cache_lock:
        _cache.clear()
        _last_touch.clear()
