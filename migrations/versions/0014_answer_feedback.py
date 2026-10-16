"""answer_feedback + chat_history.meta (LLM-12).

Revision ID: 0014_answer_feedback
Revises: 0013_llm_metering
Create Date: 2026-10-16

Thumbs up/down and "report a wrong number" on any answer, linked to the
turn that produced it (turns.turn_id -> trace, prompts, models). One rating
per user per answer; changing your mind updates it.

chat_history.meta holds per-message metadata that must not be part of the
text fed back to the model: the turn id and the route badge.
"""

from alembic import op

revision = "0014_answer_feedback"
down_revision = "0013_llm_metering"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS meta JSONB")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS answer_feedback (
            id          BIGSERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            turn_id     TEXT NOT NULL,
            session_id  INTEGER,
            rating      SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
            reason      TEXT,
            free_text   TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            triaged_at  TIMESTAMPTZ,
            UNIQUE (user_id, turn_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_rating_time ON answer_feedback (rating, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS answer_feedback")
    op.execute("ALTER TABLE chat_history DROP COLUMN IF EXISTS meta")
