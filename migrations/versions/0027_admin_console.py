"""admin console: users.is_admin, users.disabled_at, feature_flags (OBS-05).

Revision ID: 0027_admin_console
Revises: 0026_chat_search
Create Date: 2026-10-17

`is_admin` gates the Admin tab and every one of its handlers (server-side).
`disabled_at` locks an account: it can't log in, and its sessions are revoked
when it is disabled. `feature_flags` holds the operator's runtime switches
(utils/flags.py). Expand-only: new nullable/defaulted columns and a new table.
"""

from alembic import op

revision = "0027_admin_console"
down_revision = "0026_chat_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS feature_flags (
            key         TEXT PRIMARY KEY,
            enabled     BOOLEAN NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by  INTEGER
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feature_flags")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS disabled_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_admin")
