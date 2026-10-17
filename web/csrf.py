"""
web/csrf.py — double-submit CSRF tokens for the auth forms.

A random token is set in a SameSite=Strict cookie when a form is rendered and
repeated in a hidden field; a POST is accepted only when both match. A
cross-site page can make the browser POST, but cannot read the cookie to
copy it into the form.
"""

from __future__ import annotations

import secrets

from fastapi import Request, Response

import config

CSRF_COOKIE = "aria_csrf"


def issue(request: Request, response: Response) -> str:
    """Reuse the visitor's CSRF token or mint one, and (re)set the cookie."""
    token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=2 * 3600,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return token


def valid(request: Request, form_token: str | None) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not form_token:
        return False
    return secrets.compare_digest(cookie, form_token)
