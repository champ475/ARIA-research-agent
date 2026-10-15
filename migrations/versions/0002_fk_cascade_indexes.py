"""User-owned rows cascade on user delete; indexes for the hot per-user queries.

Revision ID: 0002_fk_cascade_indexes
Revises: 0001_baseline
Create Date: 2026-10-14

The baseline's foreign keys were plain `REFERENCES users(id)` (NO ACTION),
so deleting a user — needed for account deletion (SEC-06) and DPDP erasure
(COMP-03) — failed on any row they owned. Each FK is re-created with
ON DELETE CASCADE. The existing constraint is looked up by table + column
rather than assumed to have Postgres' default name.

Safe on production: the old FKs were enforced, so there are no orphan rows
for the new constraints to reject. Each ALTER briefly locks its table.
"""

from alembic import op

revision = "0002_fk_cascade_indexes"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

# (table, column, referenced table)
_FKS = (
    ("sessions", "user_id", "users"),
    ("portfolio", "user_id", "users"),
    ("watchlist", "user_id", "users"),
    ("alerts", "user_id", "users"),
    ("chat_sessions", "user_id", "users"),
    ("chat_history", "user_id", "users"),
    ("chat_history", "session_id", "chat_sessions"),
    ("saved_screeners", "user_id", "users"),
    ("reports", "user_id", "users"),
    ("financial_profiles", "user_id", "users"),
    ("user_llm_config", "user_id", "users"),
    ("telegram_link_codes", "user_id", "users"),
)


def _recreate_fks(on_delete: str) -> str:
    rows = ", ".join(f"('{t}', '{c}', '{r}')" for t, c, r in _FKS)
    return f"""
    DO $$
    DECLARE
        fk record;
        old_name text;
    BEGIN
        FOR fk IN SELECT * FROM (VALUES {rows}) AS v(tbl, col, ref) LOOP
            FOR old_name IN
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
                WHERE con.contype = 'f' AND ns.nspname = current_schema()
                  AND rel.relname = fk.tbl AND att.attname = fk.col
            LOOP
                EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', fk.tbl, old_name);
            END LOOP;
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES %I (id) ON DELETE {on_delete}',
                fk.tbl, fk.tbl || '_' || fk.col || '_fkey', fk.col, fk.ref
            );
        END LOOP;
    END $$;
    """


def upgrade() -> None:
    op.execute(_recreate_fks("CASCADE"))
    op.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_session_ts ON chat_history (session_id, ts)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_reports_user_created ON reports (user_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_reports_user_created")
    op.execute("DROP INDEX IF EXISTS idx_alerts_user")
    op.execute("DROP INDEX IF EXISTS idx_chat_history_session_ts")
    op.execute(_recreate_fks("NO ACTION"))
