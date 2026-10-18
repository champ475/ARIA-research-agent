"""
web/auth_routes.py — login, signup, logout and terms pages (SEC-04).

Plain server-rendered forms, so a successful login can set the HttpOnly
session cookie on the response itself (a Gradio event can't). Templates are
Jinja2 with autoescaping; forms carry a double-submit CSRF token.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

import config
from utils import auth, flags
from utils.audit import audit
from web import captcha, csrf
from web.session import (
    clear_session_cookie,
    client_ip,
    current_session,
    current_user_id,
    session_token,
    set_session_cookie,
)

log = logging.getLogger(__name__)

router = APIRouter()

_ROOT = Path(__file__).resolve().parent.parent
_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent / "templates")),
    autoescape=select_autoescape(["html"]),
)

CSRF_FAILED = "Your form expired. Please try again."


def render(request: Request, template: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    response = HTMLResponse("", status_code=status_code)
    ctx["csrf"] = csrf.issue(request, response)
    ctx.setdefault("title", "ARIA")
    # UX-03: the app's light/dark choice (mirrored into a cookie by ui/theme.py APP_JS),
    # applied server-side so these pages carry no script. Dark unless "light".
    ctx["theme"] = "light" if request.cookies.get("aria_theme") == "light" else "dark"
    response.body = _env.get_template(template).render(**ctx).encode("utf-8")
    response.headers["content-length"] = str(len(response.body))
    response.headers["Cache-Control"] = "no-store"
    return response


def _email_ref(email: str) -> str:
    """A failed login's target without storing the typed address itself (it
    may be someone else's email, or a password typed into the wrong box)."""
    import hashlib

    return "email-sha256:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def _to_app(token: str) -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, token)
    return response


# Fixed notices for /login?n=<code> — never echo query text into the page.
_NOTICES = {
    "verified": "Email confirmed. Log in to continue.",
    "reset": "Password changed. Log in with your new password.",
    "deleted": "Your account and its data have been deleted.",
}


def _login_page(request: Request, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    ip = client_ip(request)
    if captcha.enabled() and auth.captcha_required(ip):
        ctx["captcha_site_key"] = config.TURNSTILE_SITE_KEY
    return render(request, "login.html", status_code, title="Log in", **ctx)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, n: str = "") -> Response:
    if current_user_id(request) is not None:
        return RedirectResponse("/", status_code=303)
    return _login_page(request, notice=_NOTICES.get(n))


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form("", alias="csrf"),
    captcha_token: str = Form("", alias="cf-turnstile-response"),
) -> Response:
    if not csrf.valid(request, csrf_token):
        return _login_page(request, 400, error=CSRF_FAILED, email=email)
    ip, ua = client_ip(request), request.headers.get("user-agent")
    if captcha.enabled() and auth.captcha_required(ip) and not captcha.verify(captcha_token, ip):
        return _login_page(request, 400, error="Please complete the check below and try again.", email=email)
    try:
        user_id = auth.authenticate(email[:320], password[:1024], ip=ip)
    except auth.AuthError as e:
        audit("auth.login_failed", target=_email_ref(email), ip=ip, user_agent=ua)
        return _login_page(request, 400, error=e.user_message, email=email)
    token = auth.create_session(user_id, ip=ip, user_agent=ua)
    audit("auth.login", actor_user_id=user_id, ip=ip, user_agent=ua)
    return _to_app(token)


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request) -> Response:
    if current_user_id(request) is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "signup.html", title="Sign up")


@router.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    agree: str = Form(""),
    csrf_token: str = Form("", alias="csrf"),
) -> Response:
    if not csrf.valid(request, csrf_token):
        return render(request, "signup.html", 400, title="Sign up", error=CSRF_FAILED, email=email)
    if agree != "yes":
        return render(
            request,
            "signup.html",
            400,
            title="Sign up",
            error="Please agree to the Terms of Use & Privacy Notice to sign up.",
            email=email,
        )
    ip, ua = client_ip(request), request.headers.get("user-agent")
    if not flags.enabled("signups_open"):  # OBS-05
        return render(
            request, "signup.html", 403, title="Sign up", error="Sign-up is closed for now.", email=email
        )
    try:
        user_id = auth.signup(email[:320], password[:1024], ip=ip)
    except auth.AuthError as e:
        return render(request, "signup.html", 400, title="Sign up", error=e.user_message, email=email)
    if user_id is not None:
        version = auth.accept_current_terms(user_id)
        audit("auth.signup", actor_user_id=user_id, ip=ip, user_agent=ua)
        audit(
            "auth.terms_accepted", actor_user_id=user_id, details={"version": version}, ip=ip, user_agent=ua
        )
        # SEC-06: confirm the address before the account can be used.
        auth.start_email_verification(user_id, email.strip().lower())
    # SEC-05: identical answer for a new and an already-registered address —
    # the owner of an existing account gets an email instead, and no session
    # is started either way (the user logs in after confirming).
    return render(request, "check_email.html", title="Check your email", email=email.strip().lower())


@router.post("/auth/logout")
def logout(request: Request) -> Response:
    """Ends this session and clears the cookie. POST-only: the cookie is
    SameSite=Lax, so another site can't log a user out with a cross-site POST."""
    user_id = current_user_id(request)
    auth.logout(session_token(request))
    if user_id is not None:
        audit("auth.logout", actor_user_id=user_id, ip=client_ip(request))
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


@lru_cache(maxsize=1)
def _terms_html() -> str:
    import markdown
    import nh3

    try:
        text = (_ROOT / "LEGAL.md").read_text(encoding="utf-8")
    except OSError:
        return "<p>Terms of Use &amp; Privacy Notice unavailable — contact this instance's operator.</p>"
    return nh3.clean(markdown.markdown(text, extensions=["tables"]))


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request) -> Response:
    return render(
        request,
        "page.html",
        title="Terms of Use & Privacy Notice",
        main_class="wide",
        body_class="legal",
        body_html=_terms_html(),
        link_href="/signup",
        link_text="Back to sign up",
    )


# ── re-consent after a material change (COMP-02) ─────────────────────────────


@router.get("/terms/accept", response_class=HTMLResponse)
def terms_accept_page(request: Request) -> Response:
    info = current_session(request)
    if info is None:
        return RedirectResponse("/login", status_code=303)
    if info.consent_ok:
        return RedirectResponse("/", status_code=303)
    from utils.db import terms_version_get
    from utils.terms import current_version

    row = terms_version_get(current_version()) or {}
    return render(
        request,
        "terms_accept.html",
        title="Updated terms",
        version=current_version(),
        summary=row.get("summary", ""),
    )


@router.post("/terms/accept", response_class=HTMLResponse)
def terms_accept_submit(
    request: Request, agree: str = Form(""), csrf_token: str = Form("", alias="csrf")
) -> Response:
    info = current_session(request)
    if info is None:
        return RedirectResponse("/login", status_code=303)
    if not csrf.valid(request, csrf_token) or agree != "yes":
        return RedirectResponse("/terms/accept", status_code=303)
    version = auth.accept_current_terms(info.user_id)
    audit(
        "auth.terms_accepted",
        actor_user_id=info.user_id,
        details={"version": version},
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return RedirectResponse("/", status_code=303)
