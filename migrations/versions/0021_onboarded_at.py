"""users.onboarded_at (UX-01).

Revision ID: 0021_onboarded_at
Revises: 0020_ops_alerts
Create Date: 2026-10-16

Set when a user dismisses the first-run card (what ARIA can do, its limits,
the free trial, Telegram). Existing users are treated as onboarded.
"""

from alembic import op

revision = "0021_onboarded_at"
down_revision = "0020_ops_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMPTZ")
    op.execute("UPDATE users SET onboarded_at = now() WHERE onboarded_at IS NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS onboarded_at")
