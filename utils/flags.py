"""
utils/flags.py — the operator's runtime switches (OBS-05).

A flag is on or off without a deploy. Each has a default (used when nothing is
stored, and when the database cannot be read, so a database blip never flips a
switch); the Admin tab changes them and audits it. Reads are cached for a few
seconds, so a change reaches every replica within `TTL_SECONDS`.

    signups_open   new accounts may be created (off: sign-up says it is closed)
    chat_enabled   new chat turns are accepted (off: a maintenance message)
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

TTL_SECONDS = 15.0

# key -> (default, what it does)
FLAGS: dict[str, tuple[bool, str]] = {
    "signups_open": (True, "New accounts may be created"),
    "chat_enabled": (True, "New chat turns are accepted (turn off for maintenance)"),
}

_lock = threading.Lock()
_cache: tuple[float, dict[str, bool]] | None = None


def _stored() -> dict[str, bool]:
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache is not None and now - _cache[0] < TTL_SECONDS:
            return _cache[1]
    try:
        from utils.db import flags_all

        values = flags_all()
    except Exception:
        log.warning("feature flags unreadable; using defaults", exc_info=True)
        values = {}
    with _lock:
        _cache = (now, values)
    return values


def enabled(key: str) -> bool:
    default = FLAGS[key][0]
    return _stored().get(key, default)


def all_flags() -> list[dict]:
    stored = _stored()
    return [
        {"key": k, "enabled": stored.get(k, default), "default": default, "description": text}
        for k, (default, text) in FLAGS.items()
    ]


def set_flag(key: str, value: bool, actor_user_id: int) -> None:
    """Store a flag (the caller has already checked the actor is an admin and audits)."""
    if key not in FLAGS:
        raise KeyError(key)
    from utils.db import flag_upsert

    flag_upsert(key, value, actor_user_id)
    clear_cache()


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = None
