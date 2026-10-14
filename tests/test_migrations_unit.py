"""ARCH-01 unit checks: app startup only reads the schema version, the
checkpointer issues no DDL, and the migration chain is linear."""

from unittest.mock import patch

# See tests/graph/test_checkpointer.py: import before patching ConnectionPool.
import langgraph.checkpoint.postgres  # noqa: F401
import pytest

from utils import migrations


class _Cur:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed: list[str] = []

    def execute(self, sql, params=()):
        self.executed.append(" ".join(sql.split()))

    def fetchone(self):
        return self.rows.pop(0)


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur


def _at_head(checkpointer=True):
    return [{"present": True}, {"version_num": migrations.head_revision()}, {"present": checkpointer}]


def test_check_schema_is_read_only_and_passes_at_head():
    conn = _Conn(_at_head())
    migrations.check_schema(conn)
    assert conn.cur.executed and all(s.upper().startswith("SELECT") for s in conn.cur.executed)


def test_check_schema_refuses_pre_alembic_database():
    with pytest.raises(migrations.SchemaOutOfDate, match="no recorded revision"):
        migrations.check_schema(_Conn([{"present": False}]))


def test_check_schema_requires_checkpoint_tables():
    with pytest.raises(migrations.SchemaOutOfDate, match="checkpoint"):
        migrations.check_schema(_Conn(_at_head(checkpointer=False)))


def test_ensure_schema_never_migrates_unless_asked(monkeypatch):
    import utils.db as db

    monkeypatch.delenv("RUN_MIGRATIONS_ON_START", raising=False)
    conn = _Conn(_at_head())
    with (
        patch.object(db, "get_conn", return_value=conn),
        patch.object(db, "release_conn"),
        patch.object(migrations, "upgrade") as up,
        patch.object(migrations, "setup_checkpointer_tables") as cp,
    ):
        db.ensure_schema()
    up.assert_not_called()
    cp.assert_not_called()


def test_ensure_schema_migrates_first_when_flag_set(monkeypatch):
    import utils.db as db

    monkeypatch.setenv("RUN_MIGRATIONS_ON_START", "true")
    with (
        patch.object(db, "get_conn", return_value=_Conn(_at_head())),
        patch.object(db, "release_conn"),
        patch.object(migrations, "upgrade") as up,
        patch.object(migrations, "setup_checkpointer_tables") as cp,
    ):
        db.ensure_schema()
    up.assert_called_once()
    cp.assert_called_once()


def test_revision_chain_is_linear():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrations._alembic_config())
    assert len(script.get_heads()) == 1
    assert list(script.walk_revisions())[-1].down_revision is None  # exactly one baseline
