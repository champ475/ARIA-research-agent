"""
web/session.py — the session cookie and how a request becomes a user (SEC-04).

The cookie holds a random token (HttpOnly, SameSite=Lax, Secure outside
dev/test); the database holds only its hash. Gradio is mounted with
`gradio_auth_dependency`, so EVERY Gradio route — the page config, the queue,
file downloads — resolves the user from the cookie on each request and
answers 401 once the session is expired, logged out or revoked.
"""

from __future__ import annotations

import logging

from fastapi import Request, Response

import config
from utils.auth import SESSION_TTL_HOURS, resolve_session

log = logging.getLogger(__name__)

COOKIE_NAME = "aria_session"


def session_token(request: Request) -> str | None:
    return request.cookies.get(COOKIE_NAME) or None


def current_user_id(request: Request) -> int | None:
    """The logged-in user for this request, or None. A database outage reads
    as logged out (the request is refused), never as someone else."""
    try:
        return resolve_session(session_token(request))
    except Exception:
        log.exception("[session] could not resolve the session cookie")
        return None


def gradio_auth_dependency(request: Request) -> str | None:
    """mount_gradio_app(auth_dependency=...): the user id as a string (it
    becomes gr.Request.username), or None to refuse the request with 401."""
    uid = current_user_id(request)
    return str(uid) if uid is not None else None


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=config.COOKIE_SECURE, samesite="lax")


def client_ip(request: Request) -> str | None:
    """The connecting client's address (SEC-05 extends this for trusted proxies)."""
    return request.client.host if request.client else None
