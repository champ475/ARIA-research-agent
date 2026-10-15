"""Email verification and single-use auth tokens (SEC-06).

Revision ID: 0009_email_verification_tokens
Revises: 0008_audit_log
Create Date: 2026-10-15

`users.email_verified_at` gates chat for new accounts; accounts that already
exist are treated as verified (they signed up before verification existed).

`auth_tokens` holds email-verification and password-reset tokens, stored as
sha256 hashes, single use (`used_at`) and short-lived (`expires_at`).
"""

from alembic import op

revision = "0009_email_verification_tokens"
down_revision = "0008_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ")
    op.execute("UPDATE users SET email_verified_at = COALESCE(accepted_terms_at, created_at, now())")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_tokens (
            id          BIGSERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            purpose     TEXT NOT NULL CHECK (purpose IN ('verify', 'reset')),
            token_hash  TEXT NOT NULL UNIQUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at  TIMESTAMPTZ NOT NULL,
            used_at     TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens (user_id, purpose)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth_tokens")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS email_verified_at")
