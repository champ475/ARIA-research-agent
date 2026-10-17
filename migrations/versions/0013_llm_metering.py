"""Per-turn LLM cost/latency metering and the trial token budget (LLM-11, SEC-12).

Revision ID: 0013_llm_metering
Revises: 0012_turn_parse_failures
Create Date: 2026-10-16

turns: estimated rupee cost, the models used, and calls / tokens / ms / cost
per graph node — OBS-03 charts p50/p95 per intent from these.
users: today's LLM token spend on the operator's trial keys, capped by
TRIAL_DAILY_TOKENS (one free message can cost 5-9 model calls, so the
message count alone doesn't bound spend).
"""

from alembic import op

revision = "0013_llm_metering"
down_revision = "0012_turn_parse_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE turns ADD COLUMN IF NOT EXISTS cost_inr NUMERIC(12,6) NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE turns ADD COLUMN IF NOT EXISTS models TEXT[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE turns ADD COLUMN IF NOT EXISTS llm_by_node JSONB")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_tokens_used INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_tokens_day DATE")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS trial_tokens_day")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS trial_tokens_used")
    op.execute("ALTER TABLE turns DROP COLUMN IF EXISTS llm_by_node")
    op.execute("ALTER TABLE turns DROP COLUMN IF EXISTS models")
    op.execute("ALTER TABLE turns DROP COLUMN IF EXISTS cost_inr")
