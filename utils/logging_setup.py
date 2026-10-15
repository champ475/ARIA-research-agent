"""
utils/logging_setup.py — JSON structured logging with a per-turn context.

Every log line is one JSON object carrying UTC + IST timestamps, level, logger,
message, and the current chat turn's correlation fields (turn_id, a
pseudonymised user, session_id) so all lines from one user turn can be pulled
together. Secrets (API keys, bearer tokens, Fernet ciphertexts) are masked
before anything is written.

The turn fields live in contextvars. Gradio runs each step of a generator
handler in a freshly copied context, so a plain `with turn_context(...)` around
a generator body does not survive a `yield`; wrap the generator with
`run_in_turn(...)` instead, which drives every step inside one persistent
Context.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Any, TypeVar

import pytz

_IST = pytz.timezone("Asia/Kolkata")

turn_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("turn_id", default="")
user_var: contextvars.ContextVar[str] = contextvars.ContextVar("user", default="")
session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="")

# Ordered: the more specific patterns run first so a Groq/OpenAI key inside a
# "Bearer ..." header is masked by its own rule, not half-matched by another.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"gsk_[A-Za-z0-9]{10,}"), "gsk_***"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{10,}"), "sk-***"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "AIza***"),
    (re.compile(r"gAAAAA[A-Za-z0-9_\-=]{20,}"), "gAAAAA***"),  # Fernet token
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer ***"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)['\"]?[^\s'\",}]+"), r"\1***"),
    # Telegram bot token inside an API URL (api.telegram.org/bot<token>/...):
    # "bot123" has no word boundary between "t" and "1", so the bare-token
    # rule below can't see it (SEC-11).
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_\-]{20,}"), "bot***"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"), "***telegram-bot-token***"),
    # COMP-03 data minimisation: no email addresses in logs (the domain is
    # kept — it helps debug delivery problems without identifying anyone).
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)\b"), r"***@\1"),
]


def redact(text: str) -> str:
    """Mask every known secret shape in `text`."""
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def pseudonymise_user(user_id: Any) -> str:
    """Stable, non-reversible tag for a user id (HMAC with SECRET_KEY).

    Lets logs from one user be correlated without writing the raw id; without
    a SECRET_KEY there is nothing to key the HMAC with, so the user is 'anon'.
    """
    if user_id is None or user_id == "":
        return ""
    secret = os.getenv("SECRET_KEY", "")
    if not secret:
        return "anon"
    digest = hmac.new(secret.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
    return digest[:12]


class RedactingFilter(logging.Filter):
    """SEC-11: mask secrets in every record before any handler formats it.

    Installed on the handlers configure_logging() creates (a filter on the root
    *logger* would not see records propagated from child loggers). Bakes the
    formatted message and exception text into the record, redacted, so no
    formatter — ours or a third party's — can render the raw values."""

    _exc_formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad %-format must not lose the record
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._exc_formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record, with turn context and secrets redacted."""

    def format(self, record: logging.LogRecord) -> str:
        now_utc = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, Any] = {
            "ts_utc": now_utc.isoformat(timespec="milliseconds"),
            "ts_ist": now_utc.astimezone(_IST).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "turn_id": turn_id_var.get(),
            "user": user_var.get(),
            "session_id": session_id_var.get(),
        }
        if record.exc_text:
            payload["exc"] = redact(record.exc_text)
        elif record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False


def configure_logging(level: str | None = None, stream: Any = None) -> None:
    """Install the JSON handler on the root logger. Safe to call repeatedly;
    only the first call (or one with an explicit stream, used by tests)
    replaces handlers."""
    global _configured
    if _configured and stream is None:
        return
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel((level or os.getenv("LOG_LEVEL") or "INFO").upper())
    # Third-party chatter that would drown the app's own lines at INFO.
    for noisy in ("httpx", "httpcore", "urllib3", "yfinance", "matplotlib", "fontTools", "weasyprint"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def _set_turn(user_id: Any, session_id: Any) -> str:
    tid = uuid.uuid4().hex
    turn_id_var.set(tid)
    user_var.set(pseudonymise_user(user_id))
    session_id_var.set("" if session_id is None else str(session_id))
    return tid


@contextmanager
def turn_context(user_id: Any, session_id: Any = None) -> Iterator[str]:
    """Bind a fresh turn_id + user + session to the current context (for
    non-generator code: scripts, tests, single-call handlers)."""
    tokens = (turn_id_var.set(""), user_var.set(""), session_id_var.set(""))
    tid = _set_turn(user_id, session_id)
    try:
        yield tid
    finally:
        for var, tok in zip((turn_id_var, user_var, session_id_var), tokens, strict=True):
            # ValueError: exited in a different Context (a framework hopped
            # threads), so there is nothing to restore.
            with suppress(ValueError):
                var.reset(tok)


T = TypeVar("T")


def run_in_turn(
    gen: Generator[T, None, None], user_id: Any, session_id: Any = None
) -> Generator[T, None, None]:
    """Drive `gen` with every step running in one Context that carries this
    turn's ids, so log lines emitted after a `yield` keep the same turn_id."""
    ctx = contextvars.copy_context()
    ctx.run(_set_turn, user_id, session_id)
    try:
        while True:
            try:
                item = ctx.run(next, gen)
            except StopIteration:
                return
            yield item
    finally:
        # Consumer stopped early (client disconnect / cancel): close the inner
        # generator inside the same context so its cleanup still logs with ids.
        ctx.run(gen.close)
