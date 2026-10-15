"""
utils/mailer.py — transactional email (SEC-06): verification and password reset.

Two backends, chosen by MAIL_BACKEND:
- "console" (default; dev/test only — boot refuses it in staging/prod): the
  message is logged, so a developer can click the link from the log.
- "smtp": any SMTP server with STARTTLS. The free, zero-domain setup is a
  Gmail account with an app password:
      SMTP_HOST=smtp.gmail.com  SMTP_PORT=587  SMTP_USER=you@gmail.com
      SMTP_PASSWORD=<16-char app password>  SMTP_FROM=you@gmail.com

Links put the token in the URL *path* (/auth/verify/<token>), not a
`token=` query parameter, so the log redaction filter doesn't mask it in
console mode — and it never appears in a query string that proxies log.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

import config

log = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str


# The last messages sent by the console backend (tests read these).
outbox: list[Mail] = []


def send(to: str, subject: str, text: str) -> bool:
    """Send one plain-text email. Returns False (and logs) on failure — the
    caller's flow continues; the user can ask for the link again."""
    mail = Mail(to=to, subject=subject, text=text)
    if config.MAIL_BACKEND == "console":
        outbox.append(mail)
        del outbox[:-50]
        log.info("[mailer] (console) to=%s subject=%s\n%s", to, subject, text)
        return True
    try:
        _send_smtp(mail)
        return True
    except Exception as e:
        log.error("[mailer] SMTP send failed: %s", type(e).__name__)
        return False


def _send_smtp(mail: Mail) -> None:
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = mail.to
    msg["Subject"] = mail.subject
    msg.set_content(mail.text)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
        if config.SMTP_STARTTLS:
            smtp.starttls(context=ssl.create_default_context())
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(msg)


def link(path: str) -> str:
    return f"{config.APP_BASE_URL}{path}"


def send_verification(to: str, token: str) -> bool:
    return send(
        to,
        "Confirm your ARIA email address",
        "Welcome to ARIA.\n\n"
        f"Confirm your email address by opening this link (valid for 24 hours):\n{link('/auth/verify/' + token)}\n\n"
        "If you didn't sign up, ignore this email.",
    )


def send_password_reset(to: str, token: str) -> bool:
    return send(
        to,
        "Reset your ARIA password",
        "Someone (hopefully you) asked to reset your ARIA password.\n\n"
        f"Choose a new password here (valid for 30 minutes, one use):\n{link('/auth/reset/' + token)}\n\n"
        "If you didn't ask for this, ignore this email — your password stays the same.",
    )


def send_account_exists(to: str) -> bool:
    """SEC-05: a signup for an address that already has an account gets this
    email instead of an on-screen 'already exists' (no account enumeration)."""
    return send(
        to,
        "You already have an ARIA account",
        "Someone tried to sign up for ARIA with this email address, which already has an account.\n\n"
        f"Log in at {link('/login')}, or reset your password at {link('/auth/forgot')}.\n\n"
        "If this wasn't you, you can ignore this email.",
    )
