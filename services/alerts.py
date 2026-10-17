"""
services/alerts.py — the Alerts tab's operations (UX-06, ARCH-02).

Creating an alert happens in chat (the parser, the "already true" check and the
confirmation live in graph/). This tab is for what exists afterwards: see every
alert's condition, status, last check and delivery, pause, resume, delete, and
change the number. Edits go through the same `AlertSpec` rules as creation
(VAL-04), so an edited alert can't hold a value a new one could not.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ValidationError

from graph.alert_spec import AlertSpec
from utils.audit import audit
from utils.db import DuplicateAlert
from utils.errors import UserInputError
from utils.helpers import IST
from utils.numbers import parse_amount

__all__ = ["DuplicateAlert", "UserInputError", "delete", "edit", "rows", "set_active"]

_DELIVERY = {
    None: "—",
    "telegram": "Sent to Telegram",
    "in_app": "In your inbox",
    "pending_delivery": "Waiting in your inbox",
    "retrying": "Retrying",
}


def _when(ts: datetime | None) -> str:
    return ts.astimezone(IST).strftime("%d %b %H:%M") if ts else "—"


def _status(a: dict) -> str:
    if a.get("active"):
        return "Active"
    return "Fired (one-time)" if a.get("triggered_at") and not a.get("recurring") else "Paused"


def rows(user_id: int) -> list[dict[str, Any]]:
    """Every alert of the user, ready to show: id, symbol, condition text,
    threshold, recurring, status, last fired, last checked, delivery."""
    from utils.db import alert_list

    out = []
    for a in alert_list(user_id):
        out.append(
            {
                "id": a["id"],
                "symbol": a["symbol"],
                "condition": a.get("condition") or a["alert_type"].replace("_", " "),
                "alert_type": a["alert_type"],
                "threshold": a.get("threshold"),
                "direction": a.get("direction"),
                "recurring": bool(a.get("recurring")),
                "status": _status(a),
                "active": bool(a.get("active")),
                "last_fired": _when(a.get("triggered_at")),
                "last_checked": _when(a.get("last_checked_at")),
                "delivery": _DELIVERY.get(a.get("delivery_status"), str(a.get("delivery_status"))),
            }
        )
    return out


def set_active(user_id: int, alert_id: int, active: bool) -> bool:
    """Pause or resume. False if it isn't the user's alert. Raises DuplicateAlert
    when resuming would duplicate an active alert."""
    from utils.db import alert_get, alert_pause, alert_resume

    if alert_get(user_id, alert_id) is None:
        return False
    (alert_resume if active else alert_pause)(user_id, alert_id)
    audit("alert.resume" if active else "alert.pause", actor_user_id=user_id, target=str(alert_id))
    return True


def delete(user_id: int, alert_id: int) -> bool:
    from utils.db import alert_remove

    ok = alert_remove(user_id, alert_id)
    if ok:
        audit("alert.cancel", actor_user_id=user_id, target=str(alert_id))
    return ok


def edit(
    user_id: int,
    alert_id: int,
    threshold_text: str | None,
    recurring: bool | None = None,
    direction: str | None = None,
) -> str:
    """Change an alert's number (and optionally recurring / direction). Returns the new
    description. Raises UserInputError with a message fit to show, DuplicateAlert if it
    would equal another active alert, or LookupError if it isn't the user's alert."""
    from utils.db import alert_get, alert_update

    current = alert_get(user_id, alert_id)
    if current is None:
        raise LookupError("no such alert")
    raw = (threshold_text or "").strip()
    threshold: float | None = current.get("threshold")
    if raw:
        threshold = parse_amount(raw)
        if threshold is None:
            raise UserInputError("Enter the new number as digits, e.g. 4000 or 5%.")
    try:
        spec = AlertSpec(
            symbol=current["symbol"],
            alert_type=current["alert_type"],
            threshold=None if threshold is None else float(threshold),
            direction=direction or current.get("direction"),
            recurring=current["recurring"] if recurring is None else recurring,
            cooldown_hours=current.get("cooldown_hours") or 24,
        )
    except ValidationError as e:
        reason = str(e.errors()[0].get("msg", "invalid")).removeprefix("Value error, ")
        raise UserInputError(f"That value isn't allowed: {reason}.") from e
    alert_update(
        user_id,
        alert_id,
        spec.describe(),
        spec.threshold,
        spec.direction,
        spec.recurring,
        spec.cooldown_hours,
    )
    audit("alert.edit", actor_user_id=user_id, target=str(alert_id), details={"threshold": spec.threshold})
    return spec.describe()
