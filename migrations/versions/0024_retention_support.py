"""retention support: audit_log rows can age out after 5 years; scan indexes (COMP-06).

Revision ID: 0024_retention_support
Revises: 0023_shared_caches
Create Date: 2026-10-17

audit_log stays append-only: UPDATE is still refused, and DELETE is refused
too unless the row is more than 5 years old (the retention period in
docs/PRIVACY.md). So the retention job can age rows out, and nobody can erase
recent history. Also indexes the retention job's "oldest row" and purge scans.
Expand-only (a trigger function is replaced by a strictly more specific one).
"""

from alembic import op

revision = "0024_retention_support"
down_revision = "0023_shared_caches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND OLD.at < now() - interval '5 years' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated ON chat_sessions (updated_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_reports_created ON reports (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created ON answer_feedback (created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_feedback_created")
    op.execute("DROP INDEX IF EXISTS idx_notifications_created")
    op.execute("DROP INDEX IF EXISTS idx_reports_created")
    op.execute("DROP INDEX IF EXISTS idx_chat_sessions_updated")
    op.execute("DROP INDEX IF EXISTS idx_audit_at")
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
