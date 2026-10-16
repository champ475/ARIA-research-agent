"""downloads — generated files any replica can serve (OPS-02).

Revision ID: 0018_downloads
Revises: 0017_job_heartbeats
Create Date: 2026-10-16

Report PDFs/Markdown and screener CSVs used to be temp files on the replica
that ran the turn; the download link 404'd when the click landed on another
replica (or after a restart). Reports now re-render from `reports.content`
at /download/report/<id>; anything without a durable source (a screener's
results) is stored here for a day under an unguessable token, readable only
by its owner. Housekeeping purges expired rows.
"""

from alembic import op

revision = "0018_downloads"
down_revision = "0017_job_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            token         TEXT PRIMARY KEY,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            filename      TEXT NOT NULL,
            content_type  TEXT NOT NULL,
            content       BYTEA NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_downloads_expires ON downloads (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS downloads")
