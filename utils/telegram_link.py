"""
utils/telegram_link.py — links a user's account to their own Telegram chat,
so scripts/check_alerts.py can notify each alert's owner individually
instead of a single shared TELEGRAM_CHAT_ID.

Flow: app.py's "Connect Telegram" button calls generate_link_code(user_id),
which mints a short-lived code and shows a t.me/<bot>?start=<code> deep
link. The user taps it, Telegram opens a chat with the bot and sends
"/start <code>" automatically. poll_once() reads that update via Telegram's
getUpdates API, resolves the code back to a user_id, and stores the chat_id.
It runs as the `telegram_poller` service in worker.py (REL-05): exactly one
process holds that job's lock, because Telegram allows one getUpdates
poller per bot (two replicas used to fight with 409s) — no webhook/public HTTPS endpoint needed, which matters since
this app may run behind no public URL at all (local dev) or one that
doesn't warrant standing up a webhook route.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

import requests

from config import TELEGRAM_BOT_TOKEN
from utils.db import telegram_chat_id_set, telegram_link_code_resolve

log = logging.getLogger(__name__)

_LINK_CODE_TTL_MINUTES = 15
_POLL_TIMEOUT_S = (
    20  # Telegram long-poll — server holds the request open until an update arrives or this elapses
)
_offset = 0


def generate_link_code(user_id: int) -> str:
    """Mints a short-lived single-use code for this user's "Connect
    Telegram" deep link. Collisions are astronomically unlikely (96 bits of
    randomness) and harmless anyway (telegram_link_codes.code is the primary
    key, so a collision would just fail the insert — acceptable to let that
    surface as an error rather than defend against it)."""
    from utils.db import telegram_link_code_create

    code = secrets.token_urlsafe(12)
    expires_at = datetime.now(UTC) + timedelta(minutes=_LINK_CODE_TTL_MINUTES)
    telegram_link_code_create(user_id, code, expires_at)
    return code


def _send(chat_id, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        # The text can contain the request URL, i.e. the bot token; the log
        # filter masks it (SEC-11).
        log.warning("[telegram_link] confirmation send failed: %s", e)


def poll_once() -> None:
    global _offset
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = requests.get(  # timeout passed below  # nosec B113
        url, params={"offset": _offset, "timeout": _POLL_TIMEOUT_S}, timeout=_POLL_TIMEOUT_S + 10
    )
    resp.raise_for_status()
    for update in resp.json().get("result", []):
        _offset = update["update_id"] + 1
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or not text.startswith("/start"):
            continue
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            _send(
                chat_id,
                "Open this link from the ARIA app's Settings tab to connect — a bare /start isn't enough on its own.",
            )
            continue
        user_id = telegram_link_code_resolve(parts[1].strip())
        if user_id is None:
            _send(
                chat_id,
                "That link has expired or was already used — generate a new one in ARIA's Settings tab.",
            )
            continue
        telegram_chat_id_set(user_id, str(chat_id))
        from utils.audit import audit

        audit("settings.telegram_linked", actor_user_id=user_id)
        _send(chat_id, "✅ Connected! ARIA will send your alerts to this chat from now on.")
