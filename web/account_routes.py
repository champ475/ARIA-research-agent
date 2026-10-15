"""
web/account_routes.py — email verification, password reset and account
deletion pages (SEC-06).

    GET  /auth/verify/{token}      confirm the email address (single use, 24 h)
    GET  /auth/verify-pending      "check your inbox" + resend, for unverified sessions
    POST /auth/verify/resend       send a fresh link (rate-limited)
    GET/POST /auth/forgot          request a reset link (same answer whether or not the account exists)
    GET/POST /auth/reset/{token}   choose a new password (single use, 30 min); revokes all sessions
    GET/POST /account/delete       delete the account after re-entering the password
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from utils import auth
from utils.audit import audit
from utils.db import auth_attempt_count, auth_attempt_record, user_email
from web import csrf
from web.auth_routes import CSRF_FAILED, render
from web.session import clear_session_cookie, client_ip, current_session

log = logging.getLogger(__name__)

router = APIRouter()

_RESEND_LIMIT_PER_HOUR = 3
_FORGOT_LIMIT_PER_HOUR = 5
_FORGOT_SENT = (
    "If an account exists for that address, we've emailed a link to reset the password. "
    "It's valid for 30 minutes."
)


def _rate_ok(key: str, limit: int, window: timedelta = timedelta(hours=1)) -> bool:
    if auth_attempt_count(key, datetime.now(UTC) - window) >= limit:
        return False
    auth_attempt_record(key)
    return True


# ── email verification ────────────────────────────────────────────────────────


@router.get("/auth/verify/{token}", response_class=HTMLResponse)
def verify(request: Request, token: str) -> Response:
    user_id = auth.verify_email(token[:200])
    if user_id is None:
        return render(
            request,
            "page.html",
            400,
            title="Link expired",
            heading="This link has expired or was already used",
            error="Log in and ask for a new confirmation email.",
            link_href="/login",
            link_text="Log in",
        )
    audit("auth.email_verified", actor_user_id=user_id, ip=client_ip(request))
    if current_session(request) is not None:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?n=verified", status_code=303)


@router.get("/auth/verify-pending", response_class=HTMLResponse)
def verify_pending(request: Request, sent: int = 0) -> Response:
    info = current_session(request)
    if info is None:
        return RedirectResponse("/login", status_code=303)
    if info[1]:
        return RedirectResponse("/", status_code=303)
    return render(
        request,
        "verify_pending.html",
        title="Confirm your email",
        email=user_email(info[0]) or "",
        notice="We've sent a new link." if sent else None,
    )


@router.post("/auth/verify/resend")
def verify_resend(request: Request, csrf_token: str = Form("", alias="csrf")) -> Response:
    info = current_session(request)
    if info is None:
        return RedirectResponse("/login", status_code=303)
    if not csrf.valid(request, csrf_token):
        return RedirectResponse("/auth/verify-pending", status_code=303)
    user_id, verified = info.user_id, info.verified
    if verified:
        return RedirectResponse("/", status_code=303)
    email = user_email(user_id)
    if email and _rate_ok(f"verify:{user_id}", _RESEND_LIMIT_PER_HOUR):
        auth.start_email_verification(user_id, email)
    return RedirectResponse("/auth/verify-pending?sent=1", status_code=303)


# ── password reset ────────────────────────────────────────────────────────────


@router.get("/auth/forgot", response_class=HTMLResponse)
def forgot_page(request: Request) -> Response:
    return render(request, "forgot.html", title="Reset your password")


@router.post("/auth/forgot", response_class=HTMLResponse)
def forgot_submit(
    request: Request, email: str = Form(""), csrf_token: str = Form("", alias="csrf")
) -> Response:
    if not csrf.valid(request, csrf_token):
        return render(request, "forgot.html", 400, title="Reset your password", error=CSRF_FAILED)
    email = email.strip().lower()[:320]
    ip = client_ip(request) or "unknown"
    # Same answer whether or not the account exists, and whether or not a
    # limit was hit: the page never reveals who has an account (SEC-05).
    if email and _rate_ok(f"reset:{email}", _FORGOT_LIMIT_PER_HOUR) and _rate_ok(f"reset-ip:{ip}", 20):
        auth.request_password_reset(email)
    return render(request, "forgot.html", title="Reset your password", notice=_FORGOT_SENT, sent=True)


@router.get("/auth/reset/{token}", response_class=HTMLResponse)
def reset_page(request: Request, token: str) -> Response:
    return render(request, "reset.html", title="Choose a new password", token=token[:200])


@router.post("/auth/reset/{token}", response_class=HTMLResponse)
def reset_submit(
    request: Request,
    token: str,
    password: str = Form(""),
    confirm: str = Form(""),
    csrf_token: str = Form("", alias="csrf"),
) -> Response:
    def again(error: str) -> Response:
        return render(
            request, "reset.html", 400, title="Choose a new password", token=token[:200], error=error
        )

    if not csrf.valid(request, csrf_token):
        return again(CSRF_FAILED)
    if password != confirm:
        return again("The two passwords don't match.")
    try:
        user_id = auth.reset_password(token[:200], password)
    except auth.AuthError as e:
        return again(e.user_message)
    audit("auth.password_reset", actor_user_id=user_id, ip=client_ip(request))
    response = RedirectResponse("/login?n=reset", status_code=303)
    clear_session_cookie(response)
    return response


# ── account deletion ──────────────────────────────────────────────────────────


@router.get("/account/delete", response_class=HTMLResponse)
def delete_page(request: Request) -> Response:
    if current_session(request) is None:
        return RedirectResponse("/login", status_code=303)
    return render(request, "delete_account.html", title="Delete your account")


@router.post("/account/delete", response_class=HTMLResponse)
def delete_submit(
    request: Request,
    password: str = Form(""),
    confirm: str = Form(""),
    csrf_token: str = Form("", alias="csrf"),
) -> Response:
    info = current_session(request)
    if info is None:
        return RedirectResponse("/login", status_code=303)

    def again(error: str) -> Response:
        return render(request, "delete_account.html", 400, title="Delete your account", error=error)

    if not csrf.valid(request, csrf_token):
        return again(CSRF_FAILED)
    if confirm.strip().upper() != "DELETE":
        return again("Type DELETE to confirm.")
    user_id = info[0]
    try:
        auth.delete_account(user_id, password)
    except auth.AuthError as e:
        return again(e.user_message)
    audit("account.delete", actor_user_id=user_id, ip=client_ip(request))
    log.info("[account] deleted user %s", user_id)
    response = RedirectResponse("/login?n=deleted", status_code=303)
    clear_session_cookie(response)
    return response
