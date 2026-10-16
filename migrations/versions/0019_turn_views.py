"""SQL views over `turns` for dashboards and the SLO report (OBS-03).

Revision ID: 0019_turn_views
Revises: 0018_downloads
Create Date: 2026-10-16

turn_stats_hourly  per hour and intent: volume, failures, p50/p95 latency,
                   p95 time to first output, LLM calls, cost, cache hits.
slo_daily          per IST day: turns, good turns (availability SLO) and
                   the equity p95 time to first output (latency SLO).

A failed turn is one the user didn't get an answer to because of us:
status 'error' or 'llm_unavailable'. Refusals, quota/rate gates and
cancellations are answers (or the user's choice), not outages.
"""

from alembic import op

revision = "0019_turn_views"
down_revision = "0018_downloads"
branch_labels = None
depends_on = None

FAILED = "('error', 'llm_unavailable')"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE VIEW turn_stats_hourly AS
        SELECT date_trunc('hour', created_at)                                     AS hour,
               coalesce(intent, 'unknown')                                        AS intent,
               count(*)                                                           AS turns,
               count(*) FILTER (WHERE status IN {FAILED})                         AS failed,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)            AS p50_latency_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)           AS p95_latency_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY ttft_ms)              AS p95_ttft_ms,
               sum(llm_calls)                                                     AS llm_calls,
               sum(coalesce(cost_inr, 0))                                         AS cost_inr,
               sum(tool_calls)                                                    AS tool_calls,
               sum(cache_hits)                                                    AS cache_hits
        FROM turns
        GROUP BY 1, 2
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE VIEW slo_daily AS
        SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date                     AS day,
               count(*)                                                           AS turns,
               count(*) FILTER (WHERE status NOT IN {FAILED})                     AS good,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY ttft_ms)
                   FILTER (WHERE intent = 'equity' AND ttft_ms IS NOT NULL)       AS equity_p95_ttft_ms
        FROM turns
        GROUP BY 1
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS slo_daily")
    op.execute("DROP VIEW IF EXISTS turn_stats_hourly")
