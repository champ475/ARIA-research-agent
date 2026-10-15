"""chat_sessions: pending confirmation survives restarts and replicas (VAL-10, OPS-02).

Revision ID: 0011_session_pending
Revises: 0010_terms_versions
Create Date: 2026-10-16

When the research graph pauses for a yes/no, the session row records the
paused LangGraph thread. Any app process (after a restart, or another
replica) rebuilding that chat's state can resume the right thread; a second
browser tab on a *different* chat has its own row and never sees it.
"""

from alembic import op

revision = "0011_session_pending"
down_revision = "0010_terms_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS pending_thread_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE chat_sessions DROP COLUMN IF EXISTS pending_thread_id")
