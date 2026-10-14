"""mf_metrics: cached mutual fund performance metrics (DATA-04).

Revision ID: 0006_mf_metrics
Revises: 0005_alert_direction_unique
Create Date: 2026-10-14

One row per scheme, recomputed from its NAV history at most daily
(tools/data/mf_data.py). Full NAV histories aren't stored: thousands of
schemes x years of daily NAVs would outgrow the free Neon tier, and the
metrics are all the rankings need.
"""

from alembic import op

revision = "0006_mf_metrics"
down_revision = "0005_alert_direction_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mf_metrics (
            scheme_code     TEXT PRIMARY KEY,
            scheme_name     TEXT NOT NULL,
            category        TEXT NOT NULL,
            nav             DOUBLE PRECISION,
            nav_date        DATE,
            history_start   DATE,
            cagr_1y         DOUBLE PRECISION,
            cagr_3y         DOUBLE PRECISION,
            cagr_5y         DOUBLE PRECISION,
            volatility_1y   DOUBLE PRECISION,
            max_drawdown_3y DOUBLE PRECISION,
            consistency_1y  DOUBLE PRECISION,
            computed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_mf_metrics_category ON mf_metrics (category)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mf_metrics")
