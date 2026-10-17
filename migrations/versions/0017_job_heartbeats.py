"""job_heartbeats — one row per background job (REL-05).

Revision ID: 0017_job_heartbeats
Revises: 0016_portfolio_numeric
Create Date: 2026-10-16

utils/jobs.py runs each job under a Postgres advisory lock (one owner across
every worker, app replica and the GitHub Actions alert check) and records
here when it last started and finished, how, and on which host — the ops
watchdog (OBS-04) alerts when a heartbeat goes stale.
"""

from alembic import op

revision = "0017_job_heartbeats"
down_revision = "0016_portfolio_numeric"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS job_heartbeats (
            job               TEXT PRIMARY KEY,
            owner             TEXT,
            last_started_at   TIMESTAMPTZ,
            last_finished_at  TIMESTAMPTZ,
            last_ok_at        TIMESTAMPTZ,
            last_status       TEXT,
            last_error        TEXT,
            last_duration_ms  INTEGER,
            runs              BIGINT NOT NULL DEFAULT 0,
            failures          BIGINT NOT NULL DEFAULT 0
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS job_heartbeats")
