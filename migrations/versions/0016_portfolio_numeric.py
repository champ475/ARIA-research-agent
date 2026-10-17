"""portfolio: exact quantities and prices, buy date, sanity checks (UX-05).

Revision ID: 0016_portfolio_numeric
Revises: 0015_fundamentals_snapshot
Create Date: 2026-10-16

The Portfolio tab makes this table live. Money and quantities were REAL
(float4: ~7 significant digits, so ₹12,34,567.89 couldn't be stored
exactly) — now NUMERIC(18,4), the DATA-09 slice for this table. Each row is
one lot; holdings are lots aggregated by symbol. `buy_date` is optional.
"""

from alembic import op

revision = "0016_portfolio_numeric"
down_revision = "0015_fundamentals_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE portfolio
            ALTER COLUMN qty TYPE NUMERIC(18,4) USING round(qty::numeric, 4),
            ALTER COLUMN avg_price TYPE NUMERIC(18,4) USING round(avg_price::numeric, 4),
            ADD COLUMN IF NOT EXISTS buy_date DATE
        """
    )
    op.execute("DELETE FROM portfolio WHERE qty <= 0 OR avg_price < 0")
    op.execute("ALTER TABLE portfolio ADD CONSTRAINT portfolio_qty_positive CHECK (qty > 0)")
    op.execute("ALTER TABLE portfolio ADD CONSTRAINT portfolio_price_nonnegative CHECK (avg_price >= 0)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_user_symbol ON portfolio (user_id, symbol)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_portfolio_user_symbol")
    op.execute("ALTER TABLE portfolio DROP CONSTRAINT IF EXISTS portfolio_price_nonnegative")
    op.execute("ALTER TABLE portfolio DROP CONSTRAINT IF EXISTS portfolio_qty_positive")
    op.execute(
        """
        ALTER TABLE portfolio
            DROP COLUMN IF EXISTS buy_date,
            ALTER COLUMN qty TYPE REAL,
            ALTER COLUMN avg_price TYPE REAL
        """
    )
