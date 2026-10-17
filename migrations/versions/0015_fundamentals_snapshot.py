"""fundamentals_snapshot — screen fields for the whole NSE universe (DATA-05).

Revision ID: 0015_fundamentals_snapshot
Revises: 0014_answer_feedback
Create Date: 2026-10-16

scripts/refresh_fundamentals.py (nightly, resumable) writes one row per
EQ-series symbol; the screener queries it in SQL instead of fetching ~30
stocks live per question. Units match the screener's filters: percentages
as numbers (18.5 = 18.5%), market cap in ₹ crore, D/E as a ratio. Lenders
(banks, NBFCs) have NULL ROCE and D/E (DATA-03).
"""

from alembic import op

revision = "0015_fundamentals_snapshot"
down_revision = "0014_answer_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
            symbol          TEXT PRIMARY KEY,
            isin            TEXT,
            name            TEXT NOT NULL,
            sector          TEXT,
            industry        TEXT,
            lender          BOOLEAN NOT NULL DEFAULT FALSE,
            price           DOUBLE PRECISION,
            mcap_cr         DOUBLE PRECISION,
            pe              DOUBLE PRECISION,
            pb              DOUBLE PRECISION,
            roe             DOUBLE PRECISION,
            roce            DOUBLE PRECISION,
            de              DOUBLE PRECISION,
            div_yield       DOUBLE PRECISION,
            rev_growth      DOUBLE PRECISION,
            from_52w_high   DOUBLE PRECISION,
            from_52w_low    DOUBLE PRECISION,
            as_of           TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_fundamentals_sector ON fundamentals_snapshot (sector)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_fundamentals_mcap ON fundamentals_snapshot (mcap_cr DESC NULLS LAST)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_fundamentals_as_of ON fundamentals_snapshot (as_of)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fundamentals_snapshot")
