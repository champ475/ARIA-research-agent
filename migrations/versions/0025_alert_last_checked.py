"""alerts.last_checked_at: when the checker last evaluated an alert (UX-06, OBS-05).

Revision ID: 0025_alert_last_checked
Revises: 0024_retention_support
Create Date: 2026-10-17

"My alert didn't fire" needs an answer other than "I can't tell": the alert
check stamps every alert it evaluated, in one UPDATE per run. Expand-only.
"""

from alembic import op

revision = "0025_alert_last_checked"
down_revision = "0024_retention_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE alerts DROP COLUMN IF EXISTS last_checked_at")
