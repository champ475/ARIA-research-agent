"""
web/captcha.py — optional Cloudflare Turnstile check (SEC-05).

Off unless TURNSTILE_SITE_KEY and TURNSTILE_SECRET are set. When on, the
login form shows the widget once an IP has failed a few logins in the last
hour, and the POST is refused unless Turnstile vouches for the token.
"""

from __future__ import annotations

import logging

import config

log = logging.getLogger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def enabled() -> bool:
    return bool(config.TURNSTILE_SITE_KEY and config.TURNSTILE_SECRET)


def verify(token: str | None, ip: str | None) -> bool:
    """True if Turnstile accepts the token. Fails closed on network errors:
    the CAPTCHA is only asked for after repeated failures, so refusing one
    more attempt is the safe side."""
    if not token:
        return False
    from utils import http

    try:
        resp = http.session("turnstile").post(
            VERIFY_URL,
            data={"secret": config.TURNSTILE_SECRET, "response": token[:2048], "remoteip": ip or ""},
            timeout=(3, 10),
        )
        return bool(resp.json().get("success"))
    except Exception as e:
        log.warning("[captcha] verification call failed: %s", type(e).__name__)
        return False
