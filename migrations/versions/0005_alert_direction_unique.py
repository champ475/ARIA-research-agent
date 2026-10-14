"""alerts: a direction for percent alerts, and no duplicate active alerts (VAL-04).

Revision ID: 0005_alert_direction_unique
Revises: 0004_turns
Create Date: 2026-10-14

- `direction` (up | down | either) — pct_change used abs(), so a "drops 5%"
  alert fired on a 5% rise. Existing pct_change / portfolio_pnl rows keep
  that behaviour explicitly as 'either'.
- A partial unique index stops the same active alert being created twice.
  Existing duplicates (same user, symbol, type, threshold, direction, both
  active) are first reduced to the oldest one by deactivating the rest —
  nothing is deleted, and a deactivated duplicate can be resumed later only
  if the original is gone.
"""

from alembic import op

revision = "0005_alert_direction_unique"
down_revision = "0004_turns"
branch_labels = None
depends_on = None

_KEY = "user_id, symbol, alert_type, COALESCE(threshold, -1), COALESCE(direction, '')"


def upgrade() -> None:
    op.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS direction TEXT")
    op.execute(
        "UPDATE alerts SET direction = 'either' "
        "WHERE alert_type IN ('pct_change', 'portfolio_pnl') AND direction IS NULL"
    )
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE alerts ADD CONSTRAINT alerts_direction_check
                CHECK (direction IS NULL OR direction IN ('up', 'down', 'either'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        f"""
        UPDATE alerts a SET active = FALSE
        FROM (
            SELECT id, row_number() OVER (PARTITION BY {_KEY} ORDER BY id) AS rn
            FROM alerts WHERE active
        ) d
        WHERE a.id = d.id AND d.rn > 1
        """
    )
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_active_spec ON alerts ({_KEY}) WHERE active")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_alerts_active_spec")
    op.execute("ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_direction_check")
    op.execute("ALTER TABLE alerts DROP COLUMN IF EXISTS direction")
