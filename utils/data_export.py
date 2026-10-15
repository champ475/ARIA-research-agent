"""
utils/data_export.py — "Download my data" (COMP-03, India's DPDP Act right of access).

Every table with a `user_id` column is discovered from the live schema, so a
table added later is exported automatically (and a test fails if one is
ever skipped). Secrets are never exported: password and token hashes and
the encrypted BYO key are replaced by a note.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from utils import db

# Columns whose value must never leave the server, even to the account owner.
SECRET_COLUMNS = {"password_hash", "token_hash", "api_key_encrypted"}
_WITHHELD = "[withheld: security secret]"


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (_WITHHELD if k in SECRET_COLUMNS and v is not None else v) for k, v in row.items()}


def export_user(user_id: int) -> dict[str, Any]:
    """Everything stored about `user_id`, as JSON-ready data."""
    account = db.user_account_row(user_id)
    if account is None:
        raise LookupError("no such user")
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in db.user_scoped_tables():
        tables[table] = [_clean(r) for r in db.user_rows(table, user_id)]
    tables["audit_log"] = db.audit_log_for(user_id, limit=10_000)
    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "format": "aria-export/1",
        "account": _clean(account),
        "tables": tables,
        "notes": [
            "Secret values (password and token hashes, your encrypted API key) are withheld.",
            "Messages you sent to an LLM provider are also subject to that provider's own retention.",
        ],
    }
