"""shared OHLCV cache and bhavcopy history in Postgres (DATA-10).

Revision ID: 0023_shared_caches
Revises: 0022_alert_threshold_numeric
Create Date: 2026-10-17

The Parquet OHLCV cache and the rolling bhavcopy history lived on the app's
local disk: wiped on every restart of a container, and not shared between
replicas, so the bhavcopy fallback (built to survive a yfinance outage) lost
its memory exactly when it was needed. Both now live in Postgres. They are
shared market data, not tenant data, so there is no user_id.

Also adds the indexes the nightly retention job (utils/retention.py) scans by.
Expand-only: new tables and indexes.
"""

from alembic import op

revision = "0023_shared_caches"
down_revision = "0022_alert_threshold_numeric"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ohlcv_cache (
            symbol      TEXT NOT NULL,
            period      TEXT NOT NULL,
            interval    TEXT NOT NULL,
            fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            data        BYTEA NOT NULL,
            PRIMARY KEY (symbol, period, interval)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_ohlcv_cache_fetched ON ohlcv_cache (fetched_at)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bhavcopy_bars (
            symbol  TEXT NOT NULL,
            day     DATE NOT NULL,
            open    NUMERIC(18,4) NOT NULL,
            high    NUMERIC(18,4) NOT NULL,
            low     NUMERIC(18,4) NOT NULL,
            close   NUMERIC(18,4) NOT NULL,
            PRIMARY KEY (symbol, day)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_evidence_cache_fetched ON evidence_cache (fetched_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_pdf_cache_fetched ON pdf_cache (fetched_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_pdf_cache_fetched")
    op.execute("DROP INDEX IF EXISTS idx_evidence_cache_fetched")
    op.execute("DROP TABLE IF EXISTS bhavcopy_bars")
    op.execute("DROP INDEX IF EXISTS idx_ohlcv_cache_fetched")
    op.execute("DROP TABLE IF EXISTS ohlcv_cache")
