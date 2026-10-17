"""audit_log: append-only record of security- and money-relevant actions (COMP-04).

Revision ID: 0008_audit_log
Revises: 0007_hashed_auth_sessions
Create Date: 2026-10-15

Who did what, when, from where: logins (ok and failed), logouts, key
changes, alert create/cancel/pause/resume, report and plan generation, data
export, account deletion, terms acceptance.

Append-only is enforced by the database, not by convention: a trigger
rejects every UPDATE and DELETE (whatever role runs them), and UPDATE/DELETE
are revoked from PUBLIC. `actor_user_id` deliberately has no foreign key —
the record of what an account did must survive the account's deletion.
"""

from alembic import op

revision = "0008_audit_log"
down_revision = "0007_hashed_auth_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id             BIGSERIAL PRIMARY KEY,
            at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            actor_user_id  INTEGER,
            action         TEXT NOT NULL,
            target         TEXT,
            details        JSONB,
            ip             TEXT,
            user_agent     TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor_at ON audit_log (actor_user_id, at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_action_at ON audit_log (action, at DESC)")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log")
    op.execute(
        "CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()"
    )
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
    op.execute("DROP TABLE IF EXISTS audit_log")
