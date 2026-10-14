"""turns: one row per chat turn (OBS-02).

Revision ID: 0004_turns
Revises: 0003_pdf_cache_period_not_null
Create Date: 2026-10-14

The product-analytics and support record of every turn, written whether or
not LangSmith tracing is on: an error ID a user reports (the turn_id prefix)
finds its row, and trace_url links to the full trace. user_id cascades so
deleting an account deletes its turns.
"""

from alembic import op

revision = "0004_turns"
down_revision = "0003_pdf_cache_period_not_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS turns (
            turn_id          UUID PRIMARY KEY,
            user_id          INTEGER REFERENCES users(id) ON DELETE CASCADE,
            session_id       TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            intent           TEXT,
            complexity       TEXT,
            status           TEXT NOT NULL,
            error_id         TEXT NOT NULL,
            latency_ms       INTEGER,
            ttft_ms          INTEGER,
            llm_calls        INTEGER NOT NULL DEFAULT 0,
            tokens_in        INTEGER NOT NULL DEFAULT 0,
            tokens_out       INTEGER NOT NULL DEFAULT 0,
            tokens_estimated BOOLEAN NOT NULL DEFAULT FALSE,
            providers        TEXT[] NOT NULL DEFAULT '{}',
            prompt_versions  JSONB NOT NULL DEFAULT '{}',
            tool_calls       INTEGER NOT NULL DEFAULT 0,
            cache_hits       INTEGER NOT NULL DEFAULT 0,
            node_ms          JSONB NOT NULL DEFAULT '{}',
            data_gaps        INTEGER NOT NULL DEFAULT 0,
            grounding        JSONB,
            trace_url        TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_turns_error_id ON turns (error_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_turns_user_created ON turns (user_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_turns_created ON turns (created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS turns")
