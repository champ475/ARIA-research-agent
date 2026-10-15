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

# token hash -> (user_id, email verified)
_cache: TTLCache[str, tuple[int, bool]] = TTLCache(maxsize=10_000, ttl=SESSION_CACHE_SECONDS)
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
    check_new_password(password)
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


def session_info(token: str | None) -> tuple[int, bool] | None:
    """(user_id, email_verified) behind a session cookie, or None if missing,
    unknown, expired or logged out."""
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
    info = (int(row["user_id"]), bool(row.get("verified", True)))
    with _cache_lock:
        _cache[th] = info
        touch = th not in _last_touch
        if touch:
            _last_touch[th] = time.monotonic()
    if touch:
        db.auth_session_touch(th)
    return info


def resolve_session(token: str | None) -> int | None:
    """The user id behind a session cookie, or None if missing, unknown,
    expired or logged out."""
    info = session_info(token)
    return info[0] if info else None


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
    _forget_user(user_id, keep=keep)
    return n


def _forget_user(user_id: int, keep: str | None = None) -> None:
    with _cache_lock:
        for th in [k for k, (uid, _v) in _cache.items() if uid == user_id and k != keep]:
            _cache.pop(th, None)


def clear_session_cache() -> None:
    """Tests / after bulk revocation."""
    with _cache_lock:
        _cache.clear()
        _last_touch.clear()


# ── Email verification, password reset, account deletion (SEC-06) ────────────

VERIFY_TOKEN_TTL = timedelta(hours=24)
RESET_TOKEN_TTL = timedelta(minutes=30)
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


def _issue_token(user_id: int, purpose: str, ttl: timedelta) -> str:
    token = secrets.token_urlsafe(32)
    db.auth_token_create(user_id, purpose, hash_token(token), datetime.now(UTC) + ttl)
    return token


def start_email_verification(user_id: int, email: str) -> bool:
    """Email a single-use verification link. False if sending failed."""
    from utils import mailer

    return mailer.send_verification(email, _issue_token(user_id, "verify", VERIFY_TOKEN_TTL))


def verify_email(token: str) -> int | None:
    """Consume a verification token; the verified user's id, or None for an
    unknown, used or expired token."""
    user_id = db.auth_token_consume("verify", hash_token(token or ""))
    if user_id is None:
        return None
    db.user_mark_verified(user_id)
    _forget_user(user_id)  # cached sessions pick up "verified" immediately
    return user_id


def request_password_reset(email: str) -> None:
    """Email a reset link if the address has an account. Says nothing either
    way (no account enumeration); the caller shows one generic message."""
    from utils import mailer

    user = db.user_by_email(email)
    if user is None:
        return
    mailer.send_password_reset(user["email"], _issue_token(user["id"], "reset", RESET_TOKEN_TTL))


def check_new_password(password: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AuthError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise AuthError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters.")


def reset_password(token: str, new_password: str) -> int:
    """Set a new password with a reset token. Every existing session of the
    account is revoked. Raises AuthError for a bad password or token."""
    check_new_password(new_password)
    user_id = db.auth_token_consume("reset", hash_token(token or ""))
    if user_id is None:
        raise AuthError("This reset link is invalid, already used or expired. Ask for a new one.")
    db.user_set_password(user_id, _hash_password(new_password))
    db.user_mark_verified(user_id)  # they proved they read the inbox
    _forget_user(user_id)
    return user_id


def delete_account(user_id: int, password: str) -> None:
    """Self-serve deletion after re-entering the password. Removes the user
    and every row they own (one transaction); sessions go with them."""
    stored = db.user_password_hash(user_id)
    if stored is None or not _verify_password(password[: PASSWORD_MAX_LENGTH * 8], stored):
        raise AuthError("That password is not correct.")
    db.user_delete(user_id)
    _forget_user(user_id)
