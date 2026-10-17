"""turns.parse_failures: structured-output failures per node (LLM-05).

Revision ID: 0012_turn_parse_failures
Revises: 0011_session_pending
Create Date: 2026-10-16

{"query_analyzer": 1, ...} — how many times a node's JSON reply was still
unusable after the one repair retry on this turn. OBS-03 computes the
per-node parse-failure rate from it (target < 1%).
"""

from alembic import op

revision = "0012_turn_parse_failures"
down_revision = "0011_session_pending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE turns ADD COLUMN IF NOT EXISTS parse_failures JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE turns DROP COLUMN IF EXISTS parse_failures")
