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

from utils import auth
from web import csrf
from web.session import clear_session_cookie, client_ip, current_user_id, session_token, set_session_cookie

log = logging.getLogger(__name__)

router = APIRouter()

_ROOT = Path(__file__).resolve().parent.parent
_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent / "templates")),
    autoescape=select_autoescape(["html"]),
)

_CSRF_FAILED = "Your form expired. Please try again."


def render(request: Request, template: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    response = HTMLResponse("", status_code=status_code)
    ctx["csrf"] = csrf.issue(request, response)
    ctx.setdefault("title", "ARIA")
    response.body = _env.get_template(template).render(**ctx).encode("utf-8")
    response.headers["content-length"] = str(len(response.body))
    response.headers["Cache-Control"] = "no-store"
    return response


def _to_app(token: str) -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, token)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, notice: str | None = None) -> Response:
    if current_user_id(request) is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", title="Log in", notice=notice)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form("", alias="csrf"),
) -> Response:
    if not csrf.valid(request, csrf_token):
        return render(request, "login.html", 400, title="Log in", error=_CSRF_FAILED, email=email)
    try:
        user_id = auth.authenticate(email[:320], password[:1024])
    except auth.AuthError as e:
        return render(request, "login.html", 400, title="Log in", error=e.user_message, email=email)
    token = auth.create_session(user_id, ip=client_ip(request), user_agent=request.headers.get("user-agent"))
    log.info("[auth] login ok")
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
        return render(request, "signup.html", 400, title="Sign up", error=_CSRF_FAILED, email=email)
    if agree != "yes":
        return render(
            request,
            "signup.html",
            400,
            title="Sign up",
            error="Please agree to the Terms of Use & Privacy Notice to sign up.",
            email=email,
        )
    try:
        user_id = auth.signup(email[:320], password[:1024])
    except auth.AuthError as e:
        return render(request, "signup.html", 400, title="Sign up", error=e.user_message, email=email)
    from utils.db import accept_terms

    accept_terms(user_id)
    token = auth.create_session(user_id, ip=client_ip(request), user_agent=request.headers.get("user-agent"))
    return _to_app(token)


@router.post("/auth/logout")
def logout(request: Request) -> Response:
    """Ends this session and clears the cookie. POST-only: the cookie is
    SameSite=Lax, so another site can't log a user out with a cross-site POST."""
    auth.logout(session_token(request))
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
