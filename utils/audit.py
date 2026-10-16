"""
utils/audit.py — one call to record an auditable action (COMP-04).

    audit("alert.create", actor_user_id=uid, target="alert:12", details={...})

Rows go to the append-only `audit_log` table (migration 0008). Details are
summaries, never secrets: keys, passwords and tokens must not be passed (the
values are redacted again here as a backstop). Writing an audit row never
breaks the user's action — a failure is logged loudly instead.

Event names (keep this list in sync with the callers):
    auth.signup, auth.login, auth.login_failed, auth.logout,
    auth.logout_other_devices, auth.terms_accepted,
    settings.llm_key_set, settings.llm_key_cleared,
    settings.telegram_linked, settings.telegram_unlinked,
    alert.create, alert.cancel, alert.pause, alert.resume, screener.save,
    report.generate, plan.generate, account.export, account.delete,
    auth.password_reset, auth.email_verified
"""

from __future__ import annotations

import json
import logging
from typing import Any

from utils.logging_setup import redact

log = logging.getLogger(__name__)

ACTIONS = frozenset(
    {
        "auth.signup",
        "auth.login",
        "auth.login_failed",
        "auth.logout",
        "auth.logout_other_devices",
        "auth.terms_accepted",
        "auth.password_reset",
        "auth.email_verified",
        "settings.llm_key_set",
        "settings.llm_key_cleared",
        "settings.telegram_linked",
        "settings.telegram_unlinked",
        "alert.create",
        "alert.cancel",
        "alert.pause",
        "alert.resume",
        "screener.save",
        "portfolio.import",
        "report.generate",
        "plan.generate",
        "account.export",
        "account.delete",
    }
)


def _safe_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not details:
        return None
    # Round-trip through redact so a stray key/token in a summary is masked;
    # an oversized summary is replaced rather than cut mid-JSON.
    text = redact(json.dumps(details, default=str))
    if len(text) > 4000:
        return {"truncated": True, "keys": sorted(details)[:20]}
    result: dict[str, Any] = json.loads(text)
    return result


def audit(
    action: str,
    *,
    actor_user_id: int | None = None,
    target: str | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Append one audit row. Never raises."""
    if action not in ACTIONS:
        log.error("[audit] unknown action %r — add it to utils/audit.py ACTIONS", action)
    try:
        from utils.db import audit_log_write

        audit_log_write(
            action,
            actor_user_id=actor_user_id,
            target=target,
            details=_safe_details(details),
            ip=ip,
            user_agent=(user_agent or "")[:300] or None,
        )
    except Exception:
        log.exception("[audit] FAILED to record %s for actor %s", action, actor_user_id)
