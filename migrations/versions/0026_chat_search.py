"""Chat history search: a full-text index on message content (UX-09).

Revision ID: 0026_chat_search
Revises: 0025_alert_last_checked
Create Date: 2026-10-17

Search by title and by what was said. The 'simple' text-search configuration
(no stemming, no stop words) suits ticker symbols and Hinglish as well as
English. Expand-only: a new index.
"""

from alembic import op

revision = "0026_chat_search"
down_revision = "0025_alert_last_checked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_history_fts ON chat_history "
        "USING GIN (to_tsvector('simple', content))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chat_history_fts")
