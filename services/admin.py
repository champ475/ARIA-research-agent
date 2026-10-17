"""
services/admin.py — what an operator can do from the Admin tab (OBS-05).

Every function takes the id of the person acting and starts with
`require_admin`: the check is here, on the server, so it holds for every
caller (the tab hides itself from non-admins too, but that is only courtesy).
Anything that looks at a user's data or changes the system is written to the
audit log (COMP-04) with who did it.

Typical support questions and where the answer is:
  "my alert didn't fire"         lookup -> alerts: active, last checked, last fired, delivery status
  "why did my quota run out"     lookup -> quota today and BYO key; turns: tokens and cost per turn
  "I can't log in"               lookup -> verified, disabled, live sessions
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from config import TRIAL_DAILY_LIMIT
from utils import flags
from utils.audit import audit
from utils.errors import AriaError
from utils.helpers import IST

log = logging.getLogger(__name__)

__all__ = [
    "Forbidden",
    "alerts_of",
    "find_user",
    "flag_list",
    "grant_admin",
    "is_admin",
    "recent_turns",
    "require_admin",
    "set_disabled",
    "set_flag",
]


class Forbidden(AriaError):
    """The caller is not an admin (HTTP 403 in spirit)."""

    default_message = "403: this area is for operators only."
    show_error_id = False
    status_code = 403


def is_admin(user_id: int | None) -> bool:
    if not user_id:
        return False
    from utils.db import user_is_admin

    try:
        return user_is_admin(int(user_id))
    except Exception:
        log.warning("admin check failed; treating as not admin", exc_info=True)
        return False


def require_admin(user_id: int | None) -> int:
    if not is_admin(user_id):
        raise Forbidden()
    return int(user_id)  # type: ignore[arg-type]


def _when(ts: datetime | None) -> str:
    return ts.astimezone(IST).strftime("%d %b %Y, %H:%M IST") if ts else "—"


def find_user(actor: int | None, email: str) -> dict[str, Any] | None:
    """One account by email: identity, signup date, quota, BYO key yes/no, Telegram, alerts, sessions."""
    admin = require_admin(actor)
    from utils.db import user_admin_view

    row = user_admin_view(email or "")
    audit("admin.lookup", actor_user_id=admin, target=str(row["id"]) if row else None)
    if row is None:
        return None
    today = datetime.now(IST).date()
    used = int(row["free_msgs_used_today"] or 0) if row["free_msgs_reset_at"] == today else 0
    return {
        "id": row["id"],
        "email": row["email"],
        "signed_up": _when(row["created_at"]),
        "verified": row["email_verified_at"] is not None,
        "disabled": row["disabled_at"] is not None,
        "is_admin": bool(row["is_admin"]),
        "quota": "own key (no daily limit)"
        if row["byo"]
        else f"{used}/{TRIAL_DAILY_LIMIT} free messages today",
        "byo": bool(row["byo"]),
        "telegram_linked": bool(row["telegram_linked"]),
        "active_alerts": int(row["active_alerts"]),
        "live_sessions": int(row["live_sessions"]),
        "last_turn": _when(row["last_turn_at"]),
    }


def recent_turns(actor: int | None, user_id: int | None = None, limit: int = 25) -> list[dict[str, Any]]:
    """The latest turns (of one user or of everyone): status, latency, tokens, cost and
    the error ID that finds the trace (scripts/find_turn.py <id>)."""
    admin = require_admin(actor)
    from utils.db import turns_recent

    rows = turns_recent(user_id, limit=max(1, min(int(limit), 100)))
    audit("admin.turns", actor_user_id=admin, target=str(user_id) if user_id else "all")
    return [
        {
            "when": _when(r["created_at"]),
            "user_id": r["user_id"],
            "intent": r["intent"] or "—",
            "status": r["status"],
            "latency_s": f"{(r['latency_ms'] or 0) / 1000:.1f}",
            "llm_calls": r["llm_calls"],
            "tokens": r["tokens"],
            "cost_inr": f"{float(r['cost_inr'] or 0):.3f}",
            "gaps": r["data_gaps"],
            "error_id": r["error_id"],
            "trace": r["trace_url"] or f"python scripts/find_turn.py {r['error_id']}",
        }
        for r in rows
    ]


def alerts_of(actor: int | None, user_id: int) -> list[dict[str, Any]]:
    """A user's alerts as support needs them: is it active, when was it last checked, last fired,
    and did the notification get through."""
    admin = require_admin(actor)
    from services.alerts import rows
    from utils.db import alert_list

    audit("admin.alerts", actor_user_id=admin, target=str(user_id))
    shown = {r["id"]: r for r in rows(user_id)}
    out = []
    for a in alert_list(user_id):
        r = shown[a["id"]]
        out.append(
            {
                "id": a["id"],
                "condition": r["condition"],
                "status": r["status"],
                "last_checked": r["last_checked"],
                "last_fired": r["last_fired"],
                "delivery": r["delivery"],
                "attempts": a.get("delivery_attempts") or 0,
                "last_error": a.get("last_delivery_error") or "—",
            }
        )
    return out


def set_disabled(actor: int | None, user_id: int, disabled: bool) -> bool:
    """Lock or unlock an account (locking ends its sessions). An admin can't lock themselves out."""
    admin = require_admin(actor)
    if disabled and user_id == admin:
        raise Forbidden("You can't disable your own account.")
    from utils.db import user_set_disabled

    ok = user_set_disabled(user_id, disabled)
    if ok:
        audit(
            "admin.user_disable" if disabled else "admin.user_enable",
            actor_user_id=admin,
            target=str(user_id),
        )
    return ok


def grant_admin(actor: int | None, user_id: int, make_admin: bool) -> bool:
    admin = require_admin(actor)
    if not make_admin and user_id == admin:
        raise Forbidden("You can't remove your own admin access.")
    from utils.db import user_set_admin

    ok = user_set_admin(user_id, make_admin)
    if ok:
        audit("admin.grant" if make_admin else "admin.revoke", actor_user_id=admin, target=str(user_id))
    return ok


def flag_list(actor: int | None) -> list[dict[str, Any]]:
    require_admin(actor)
    return flags.all_flags()


def set_flag(actor: int | None, key: str, value: bool) -> None:
    admin = require_admin(actor)
    flags.set_flag(key, value, admin)
    audit("admin.flag_set", actor_user_id=admin, target=key, details={"enabled": bool(value)})
