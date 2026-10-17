"""alerts.threshold: exact decimals (DATA-09).

Revision ID: 0022_alert_threshold_numeric
Revises: 0021_onboarded_at
Create Date: 2026-10-17

`threshold` was REAL (float4, ~7 significant digits), so a price threshold
such as 12345.6789 drifted when stored and compared. Now NUMERIC(18,4), the
same as portfolio.qty / avg_price (0016).

CONTRACT: a column type change. It is safe for a rollback to the previous
image: the old code writes floats (Postgres rounds them into NUMERIC) and
only compares or formats what it reads, and float/Decimal comparison and
`:.2f` both work. The column keeps its name; utils/db.py now hands out Decimal.
The unique index over COALESCE(threshold, -1) (0005) is rebuilt by Postgres.
"""

from alembic import op

revision = "0022_alert_threshold_numeric"
down_revision = "0021_onboarded_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE alerts ALTER COLUMN threshold TYPE NUMERIC(18,4) USING round(threshold::numeric, 4)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE alerts ALTER COLUMN threshold TYPE REAL")
