"""ops_alerts + job_heartbeats.consecutive_failures (OBS-04).

Revision ID: 0020_ops_alerts
Revises: 0019_turn_views
Create Date: 2026-10-16

The ops watchdog (utils/watchdog.py, a worker job every 5 minutes) pages
the operator when something is wrong. `ops_alerts` holds one row per alert
key so a condition that stays true pages once (then a reminder every hour),
not every 5 minutes, and a recovery sends one "resolved" message.
`consecutive_failures` lets "the data canary failed twice in a row" be a rule.
"""

from alembic import op

revision = "0020_ops_alerts"
down_revision = "0019_turn_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE job_heartbeats ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_alerts (
            key               TEXT PRIMARY KEY,
            title             TEXT NOT NULL,
            detail            TEXT,
            first_fired_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_notified_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            notifications     INTEGER NOT NULL DEFAULT 1,
            resolved_at       TIMESTAMPTZ
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops_alerts")
    op.execute("ALTER TABLE job_heartbeats DROP COLUMN IF EXISTS consecutive_failures")
