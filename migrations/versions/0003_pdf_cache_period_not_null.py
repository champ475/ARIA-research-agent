"""pdf_cache: store "no period" as '' so the unique key actually de-duplicates.

Revision ID: 0003_pdf_cache_period_not_null
Revises: 0002_fk_cascade_indexes
Create Date: 2026-10-14

UNIQUE (symbol, doc_type, period) never matched rows whose period was NULL
(NULLs are distinct in a unique constraint), so pdf_cache_set's
ON CONFLICT ... DO UPDATE never fired for period-less documents: every
refetch inserted another row and pdf_cache_get returned an arbitrary one.
Keep the newest row per (symbol, doc_type) among the NULL-period ones, then
make period NOT NULL DEFAULT ''. (NULLS NOT DISTINCT would need Postgres 15+;
'' works on every version.)
"""

from alembic import op

revision = "0003_pdf_cache_period_not_null"
down_revision = "0002_fk_cascade_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM pdf_cache p
        USING pdf_cache newer
        WHERE p.period IS NULL AND newer.period IS NULL
          AND p.symbol = newer.symbol AND p.doc_type = newer.doc_type
          AND (newer.fetched_at, newer.id) > (p.fetched_at, p.id)
        """
    )
    # A '' row may already exist alongside a NULL one only if written by code
    # newer than this migration; prefer it and drop the NULL leftover.
    op.execute(
        """
        DELETE FROM pdf_cache p
        USING pdf_cache blank
        WHERE p.period IS NULL AND blank.period = ''
          AND p.symbol = blank.symbol AND p.doc_type = blank.doc_type
        """
    )
    op.execute("UPDATE pdf_cache SET period = '' WHERE period IS NULL")
    op.execute("ALTER TABLE pdf_cache ALTER COLUMN period SET DEFAULT ''")
    op.execute("ALTER TABLE pdf_cache ALTER COLUMN period SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE pdf_cache ALTER COLUMN period DROP NOT NULL")
    op.execute("ALTER TABLE pdf_cache ALTER COLUMN period DROP DEFAULT")
    op.execute("UPDATE pdf_cache SET period = NULL WHERE period = ''")
