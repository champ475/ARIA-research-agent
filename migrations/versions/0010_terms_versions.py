"""Versioned terms and re-consent (COMP-02).

Revision ID: 0010_terms_versions
Revises: 0009_email_verification_tokens
Create Date: 2026-10-15

`terms_versions` lists every published version of LEGAL.md (its "Version:"
line). A `material` version makes every user who accepted an older one
re-accept before using the app again. `users.accepted_terms_version`
records which version each user agreed to (with `accepted_terms_at`).

Publishing new terms = edit LEGAL.md's version line AND add a migration that
inserts the matching row (a test fails if LEGAL.md names a version the table
doesn't know).
"""

from alembic import op

revision = "0010_terms_versions"
down_revision = "0009_email_verification_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS terms_versions (
            version       TEXT PRIMARY KEY,
            effective_at  TIMESTAMPTZ NOT NULL,
            material      BOOLEAN NOT NULL,
            summary       TEXT NOT NULL
        )
        """
    )
    op.execute(
        "INSERT INTO terms_versions (version, effective_at, material, summary) VALUES "
        "('2026-10-15', '2026-10-15T00:00:00+05:30', TRUE, "
        "'First versioned terms: self-serve account deletion, email verification, an audit log of "
        "account actions, and how re-consent works.') ON CONFLICT (version) DO NOTHING"
    )
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS accepted_terms_version TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS accepted_terms_version")
    op.execute("DROP TABLE IF EXISTS terms_versions")
