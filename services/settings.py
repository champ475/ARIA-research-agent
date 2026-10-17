"""
services/settings.py — the Settings tab's operations (ARCH-02): the user's own
LLM key, Telegram linking, the alert inbox and signed-in devices.

Nothing here knows about Gradio. Failures that the user should see are raised
as AriaError / return a short message; the UI renders them.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME, TRIAL_DAILY_LIMIT
from services.chat import evict_user
from utils.audit import audit
from utils.errors import AriaError
from utils.helpers import IST

log = logging.getLogger(__name__)


# ── Bring-your-own LLM key ────────────────────────────────────────────────────


def llm_status_md(user_id: int) -> str:
    from utils.db import _query, llm_config_get

    row = llm_config_get(user_id)
    if row:
        base = row["base_url"] or "api.openai.com (or provider default)"
        return f"✅ **Using your own key** — model `{row['model']}` via `{base}`."
    urow = _query(
        "SELECT free_msgs_used_today, free_msgs_reset_at FROM users WHERE id=%s",
        (user_id,),
        fetch="one",
    )
    used = urow["free_msgs_used_today"] if urow else 0
    today = datetime.now(IST).date()
    if urow and urow["free_msgs_reset_at"] != today:
        used = 0
    remaining = max(0, TRIAL_DAILY_LIMIT - used)
    return f"🆓 **Free trial** — {remaining}/{TRIAL_DAILY_LIMIT} messages left today. Add your own key below for unlimited use."


def _url_host(url: str | None) -> str | None:
    return urlsplit(url).hostname if url else None


class KeyNotSaved(Exception):
    """The key could not be stored; `message` is what the user is told."""

    def __init__(self, message: str, model: str, base_url: str, api_key: str):
        super().__init__(message)
        self.message, self.model, self.base_url, self.api_key = message, model, base_url, api_key


def save_llm_key(user_id: int, model: Any, base_url: Any, api_key: Any) -> tuple[str, str]:
    """VAL-07: format checks (SEC-01 URL policy included), then a live 1-token
    call, so a key is saved only once the provider has accepted it. Returns
    the cleaned (model, base_url). Raises KeyNotSaved with the message to show."""
    from utils.forms import llm_settings, probe_llm

    try:
        model, api_key, base_url = llm_settings(model, api_key, base_url)
        probe_llm(model, api_key, base_url)
    except AriaError as e:
        hint = f" {e.fix_hint}" if e.fix_hint else ""
        raise KeyNotSaved(f"⚠️ {e.user_message}{hint}", model or "", base_url or "", api_key or "") from e
    from utils.crypto import encrypt
    from utils.db import llm_config_set

    try:
        encrypted = encrypt(api_key)
    except ValueError as e:
        log.error(f"[Settings] encrypt() failed — SECRET_KEY misconfigured: {e}")
        raise KeyNotSaved(
            "⚠️ Server misconfiguration: keys can't be saved securely right now. "
            "Please contact the site operator.",
            model,
            base_url or "",
            api_key,
        ) from e
    llm_config_set(user_id, "openai_compatible", model, encrypted, base_url=base_url)
    audit(
        "settings.llm_key_set",
        actor_user_id=user_id,
        details={"model": model[:100], "base_url_host": _url_host(base_url)},
    )
    # Evict the cached orchestrator so the new key applies on this user's very
    # next message: a GraphOrchestrator resolves its BYO config once per instance.
    evict_user(user_id)
    return model, base_url or ""


def check_llm_key(user_id: int, model: Any, base_url: Any, api_key: Any) -> str:
    """One 1-token call with the typed (or, if the key box is empty, the saved)
    config. Reports a coarse error class, never the raw upstream text — that
    would turn this button into a way to read responses from arbitrary
    endpoints (SEC-01)."""
    from agents.llm_client import _ERROR_CLASS_TEXT, LLMProvidersExhausted, _error_class
    from agents.providers.openai_compatible import OpenAICompatibleProvider
    from utils.url_safety import UnsafeURLError

    model = (model or "").strip()
    base_url = (base_url or "").strip() or None
    api_key = (api_key or "").strip()
    if not api_key:
        from utils.crypto import decrypt
        from utils.db import llm_config_get

        row = llm_config_get(user_id)
        if row is None:
            return "⚠️ Enter a model and API key (or save one) first."
        try:
            api_key = decrypt(row["api_key_encrypted"])
        except ValueError:
            return "⚠️ Your saved key can't be read any more — please enter it again."
        model = model or row["model"]
        base_url = base_url or row["base_url"]
    if not model:
        return "⚠️ Model is required."
    try:
        provider = OpenAICompatibleProvider("Reply with OK.", api_key, model, base_url)
        provider.ask("ping", temperature=0.0, max_tokens=1)
    except UnsafeURLError as e:
        return f"⚠️ {e.user_message}"
    except Exception as e:
        kind = _error_class(LLMProvidersExhausted(e, is_byo=True))
        log.warning(f"[Settings] connection test failed ({kind}): {e}")
        return f"⚠️ Connection test failed: {_ERROR_CLASS_TEXT[kind]}"
    return "✅ Connection works — the provider answered."


def clear_llm_key(user_id: int) -> None:
    from utils.db import llm_config_clear

    llm_config_clear(user_id)
    audit("settings.llm_key_cleared", actor_user_id=user_id)
    evict_user(user_id)


# ── Telegram ──────────────────────────────────────────────────────────────────


def telegram_status_md(user_id: int) -> str:
    from utils.db import telegram_chat_id_get

    return (
        "✅ **Telegram connected** — alerts will be sent to your linked chat."
        if telegram_chat_id_get(user_id)
        else "⚪ **Not connected** — click Connect Telegram below to link your chat."
    )


def telegram_connect_md(user_id: int) -> str:
    if not TELEGRAM_BOT_TOKEN:
        return "⚠️ This ARIA instance hasn't configured a Telegram bot yet — ask the admin to set `TELEGRAM_BOT_TOKEN`."
    if not TELEGRAM_BOT_USERNAME:
        return "⚠️ Admin needs to set `TELEGRAM_BOT_USERNAME` for the connect link to work."
    from utils.telegram_link import generate_link_code

    code = generate_link_code(user_id)
    link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={code}"
    return (
        f"👉 [**Tap here to connect**]({link}) — opens Telegram, sends `/start` automatically. "
        f"Link expires in 15 minutes."
    )


def telegram_disconnect(user_id: int) -> None:
    from utils.db import telegram_chat_id_set

    telegram_chat_id_set(user_id, None)
    audit("settings.telegram_unlinked", actor_user_id=user_id)


# ── Alert inbox (DATA-08) ─────────────────────────────────────────────────────


def inbox(user_id: int) -> list[dict]:
    from utils.db import notifications_list

    return notifications_list(user_id, limit=30)


def inbox_mark_read(user_id: int) -> None:
    from utils.db import notifications_mark_read

    notifications_mark_read(user_id)


# ── Signed-in devices (SEC-04) ────────────────────────────────────────────────


def devices(user_id: int) -> list[dict]:
    from utils.db import auth_sessions_list

    return auth_sessions_list(user_id)


def logout_other_devices(user_id: int, current_token: str | None) -> int:
    from utils.auth import logout_other_devices as _logout_others

    n = _logout_others(user_id, current_token)
    audit("auth.logout_other_devices", actor_user_id=user_id, details={"revoked": n})
    return n
