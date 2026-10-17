"""sessions: hashed tokens, last seen, client info (SEC-04).

Revision ID: 0007_hashed_auth_sessions
Revises: 0006_mf_metrics
Create Date: 2026-10-15

Login sessions become real cookie sessions. The table stores only
sha256(token) — a database leak no longer hands out live sessions — plus
when the session was last used and from which client (shown in "log out
other devices", and kept for investigations).

Existing rows hold plaintext tokens that no browser has ever kept (the old
UI discarded the token after login), so they are deleted rather than
migrated; everyone simply logs in again.
"""

from alembic import op

revision = "0007_hashed_auth_sessions"
down_revision = "0006_mf_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM sessions")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS token")
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS token_hash TEXT NOT NULL")
    op.execute(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    )
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ip TEXT")
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_agent TEXT")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS sessions_token_hash_key ON sessions (token_hash)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at)")


def downgrade() -> None:
    op.execute("DELETE FROM sessions")
    op.execute("DROP INDEX IF EXISTS idx_sessions_expires")
    op.execute("DROP INDEX IF EXISTS idx_sessions_user")
    op.execute("DROP INDEX IF EXISTS sessions_token_hash_key")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS user_agent")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS ip")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS last_seen_at")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS token_hash")
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS token TEXT UNIQUE NOT NULL")
