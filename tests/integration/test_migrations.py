"""ARCH-01 on a real Postgres: migrations bring both a fresh database and a
database built by the old init_db() (the shape production is in) to head,
existing rows survive, the last migration downgrades and re-applies, and a
database that is behind is refused at startup."""

import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from utils import migrations

pytestmark = pytest.mark.integration

LEGACY_SQL = (Path(__file__).parent / "legacy_schema.sql").read_text(encoding="utf-8")


def _with_db(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@pytest.fixture
def scratch_db(pg_url):
    """A separate, empty database on the same server, dropped afterwards."""
    name = f"mig_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(pg_url)
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    yield _with_db(pg_url, name)
    admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


def _conn(url):
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


def _fk_delete_rule(conn, table: str, column: str) -> str:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT rc.delete_rule FROM information_schema.referential_constraints rc
        JOIN information_schema.key_column_usage k
          ON k.constraint_name = rc.constraint_name AND k.constraint_schema = rc.constraint_schema
        WHERE k.table_name = %s AND k.column_name = %s
        """,
        (table, column),
    )
    rows = cur.fetchall()
    assert len(rows) == 1, rows  # exactly one FK on the column, none left behind
    return rows[0]["delete_rule"]


def test_fresh_database_reaches_head(pg_db):
    conn = pg_db.get_conn()
    try:
        migrations.check_schema(conn)  # raises if not at head / no checkpoint tables
        assert migrations.current_revision(conn) == migrations.head_revision()
    finally:
        pg_db.release_conn(conn)


def test_legacy_database_upgrades_in_place_keeping_data(scratch_db):
    conn = _conn(scratch_db)
    cur = conn.cursor()
    cur.execute(LEGACY_SQL)
    cur.execute("INSERT INTO users (email, password_hash) VALUES ('old@example.com', 'x') RETURNING id")
    uid = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO alerts (user_id, symbol, alert_type, condition, threshold) "
        "VALUES (%s, 'TCS', 'price_above', 'above', 4000)",
        (uid,),
    )
    cur.execute("INSERT INTO chat_sessions (user_id, tab) VALUES (%s, 'chat') RETURNING id", (uid,))
    sid = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO chat_history (user_id, session_id, tab, role, content) VALUES (%s, %s, 'chat', 'user', 'hi')",
        (uid, sid),
    )
    assert migrations.current_revision(conn) is None  # pre-Alembic, like production

    migrations.upgrade(scratch_db)

    assert migrations.current_revision(conn) == migrations.head_revision()
    cur.execute("SELECT count(*) AS n FROM alerts WHERE user_id=%s", (uid,))
    assert cur.fetchone()["n"] == 1
    assert _fk_delete_rule(conn, "alerts", "user_id") == "CASCADE"
    assert _fk_delete_rule(conn, "chat_history", "session_id") == "CASCADE"

    # Deleting a user now removes everything they own (SEC-06 / COMP-03 erasure).
    cur.execute("DELETE FROM users WHERE id=%s", (uid,))
    for table in ("alerts", "chat_sessions", "chat_history"):
        cur.execute(f"SELECT count(*) AS n FROM {table}")  # noqa: S608 — fixed table names
        assert cur.fetchone()["n"] == 0, table
    conn.close()


def test_last_migration_downgrades_and_reapplies(scratch_db):
    migrations.upgrade(scratch_db)
    head = migrations.head_revision()
    migrations.downgrade(scratch_db, "-1")
    conn = _conn(scratch_db)
    try:
        assert migrations.current_revision(conn) != head
        with pytest.raises(migrations.SchemaOutOfDate, match="scripts/migrate.py"):
            migrations.check_schema(conn, require_checkpointer=False)
        migrations.upgrade(scratch_db)
        assert migrations.current_revision(conn) == head
    finally:
        conn.close()


def test_baseline_downgrade_refuses_without_explicit_opt_in(scratch_db, monkeypatch):
    monkeypatch.delenv("ALEMBIC_ALLOW_DESTRUCTIVE", raising=False)
    migrations.upgrade(scratch_db, "0001_baseline")
    with pytest.raises(RuntimeError, match="ALEMBIC_ALLOW_DESTRUCTIVE"):
        migrations.downgrade(scratch_db, "base")


def test_upgrade_is_idempotent(scratch_db):
    migrations.upgrade(scratch_db)
    migrations.upgrade(scratch_db)  # second run is a no-op, not an error
    conn = _conn(scratch_db)
    try:
        assert migrations.current_revision(conn) == migrations.head_revision()
    finally:
        conn.close()
