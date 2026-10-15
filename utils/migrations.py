"""
utils/migrations.py — schema version checks and the migration runner (ARCH-01).

The app never creates or alters tables while starting up. Schema changes are
Alembic revisions in migrations/versions/, applied once per deploy by
`python scripts/migrate.py` (the Docker image runs it before the app). At
startup the app only *checks* that the database is at the revision this code
expects, and refuses to start against an older schema with an instruction to
migrate, instead of failing later on a missing column.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
# Alembic logs one INFO line per internal plugin on every config load.
logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)

_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = _ROOT / "alembic.ini"


class SchemaOutOfDate(RuntimeError):
    """The database isn't at the Alembic head revision this code needs."""


def _alembic_config(url: str | None = None) -> Any:
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    if url:
        cfg.attributes["url"] = url
    return cfg


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("migrations/ has no revisions")
    return head


def current_revision(conn: Any) -> str | None:
    """The revision recorded in alembic_version, or None for a database that
    has never been migrated (fresh, or still on the pre-Alembic schema)."""
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('alembic_version') IS NOT NULL AS present")
    row = cur.fetchone()
    present = row["present"] if isinstance(row, dict) else row[0]
    if not present:
        return None
    cur.execute("SELECT version_num FROM alembic_version")
    row = cur.fetchone()
    if row is None:
        return None
    return row["version_num"] if isinstance(row, dict) else row[0]


def upgrade(url: str, revision: str = "head") -> None:
    from alembic import command

    command.upgrade(_alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    from alembic import command

    command.downgrade(_alembic_config(url), revision)


def setup_checkpointer_tables(url: str) -> None:
    """LangGraph's PostgresSaver keeps its own (versioned, idempotent) schema;
    creating it is a release step too, not something each app process does."""
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()


def run_migrations_on_start_enabled() -> bool:
    return os.getenv("RUN_MIGRATIONS_ON_START", "false").strip().lower() in ("1", "true", "yes")


_HOW_TO_FIX = (
    "Run `python scripts/migrate.py` (or set RUN_MIGRATIONS_ON_START=true for a single-instance deployment)."
)


def check_schema(conn: Any, require_checkpointer: bool = True) -> None:
    """Raise SchemaOutOfDate unless the database is at head (and, for the
    app, LangGraph's checkpoint tables exist). Read-only."""
    have, want = current_revision(conn), head_revision()
    if have != want:
        raise SchemaOutOfDate(
            f"Database schema is at {have or 'no recorded revision'}, this code needs {want}. {_HOW_TO_FIX}"
        )
    if require_checkpointer:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('checkpoint_migrations') IS NOT NULL AS present")
        row = cur.fetchone()
        if not (row["present"] if isinstance(row, dict) else row[0]):
            raise SchemaOutOfDate(f"LangGraph checkpoint tables are missing. {_HOW_TO_FIX}")
