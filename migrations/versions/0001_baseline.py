"""Baseline: the schema utils/db.py::init_db() used to create on every boot.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-14

Idempotent on purpose (IF NOT EXISTS everywhere): a database created by the
old init_db() — i.e. production — runs `alembic upgrade head` directly, no
`alembic stamp` needed, and comes out identical to a fresh one.
"""

import os

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS users (
        id            SERIAL PRIMARY KEY,
        email         TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at    TIMESTAMPTZ DEFAULT now()
    );
    -- Free-trial quota (users without a user_llm_config row get N free
    -- messages/day on the app's own Groq/Gemini keys), Telegram alert
    -- linking, and signup ToS consent record — all additive, all
    -- nullable/defaulted so existing rows need no backfill.
    ALTER TABLE users ADD COLUMN IF NOT EXISTS free_msgs_used_today INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS free_msgs_reset_at DATE;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS accepted_terms_at TIMESTAMPTZ;

    CREATE TABLE IF NOT EXISTS sessions (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        token       TEXT UNIQUE NOT NULL,
        expires_at  TIMESTAMPTZ NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS portfolio (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        symbol      TEXT NOT NULL,
        qty         REAL NOT NULL,
        avg_price   REAL NOT NULL,
        added_at    TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS watchlist (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        symbol      TEXT NOT NULL,
        added_at    TIMESTAMPTZ DEFAULT now(),
        UNIQUE(user_id, symbol)
    );

    CREATE TABLE IF NOT EXISTS alerts (
        id            SERIAL PRIMARY KEY,
        user_id       INTEGER NOT NULL REFERENCES users(id),
        symbol        TEXT NOT NULL,
        alert_type    TEXT NOT NULL CHECK (alert_type IN (
            'price_above','price_below','pct_change',
            'rsi_cross_above','rsi_cross_below',
            'macd_bullish_cross','macd_bearish_cross',
            'ma_cross','bb_breakout','volume_spike',
            'week52_high','week52_low','bulk_deal','portfolio_pnl'
        )),
        condition     TEXT,
        threshold     REAL,
        active        BOOLEAN DEFAULT TRUE,
        created_at    TIMESTAMPTZ DEFAULT now(),
        triggered_at  TIMESTAMPTZ,
        recurring     BOOLEAN DEFAULT FALSE,
        cooldown_hours INTEGER DEFAULT 24
    );
    CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts (active) WHERE active = TRUE;
    -- Migration for DBs created before recurring alerts existed.
    ALTER TABLE alerts ADD COLUMN IF NOT EXISTS recurring BOOLEAN DEFAULT FALSE;
    ALTER TABLE alerts ADD COLUMN IF NOT EXISTS cooldown_hours INTEGER DEFAULT 24;

    CREATE TABLE IF NOT EXISTS chat_sessions (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        tab         TEXT NOT NULL,
        title       TEXT NOT NULL DEFAULT 'New chat',
        created_at  TIMESTAMPTZ DEFAULT now(),
        updated_at  TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS chat_history (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        session_id  INTEGER REFERENCES chat_sessions(id),
        tab         TEXT NOT NULL,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        ts          TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS saved_screeners (
        id            SERIAL PRIMARY KEY,
        user_id       INTEGER NOT NULL REFERENCES users(id),
        name          TEXT NOT NULL,
        filters_json  TEXT NOT NULL,
        created_at    TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS reports (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        symbol      TEXT,
        report_type TEXT NOT NULL,
        title       TEXT NOT NULL,
        content     TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT now()
    );

    -- pdf_cache is a shared, symbol-keyed fetch cache (not per-tenant data),
    -- so it intentionally has no user_id.
    CREATE TABLE IF NOT EXISTS pdf_cache (
        id            SERIAL PRIMARY KEY,
        symbol        TEXT NOT NULL,
        doc_type      TEXT NOT NULL,
        period        TEXT,
        url           TEXT,
        text_content  TEXT,
        fetched_at    TIMESTAMPTZ DEFAULT now(),
        UNIQUE (symbol, doc_type, period)
    );

    -- One profile per user. Stored as a flexible JSON blob (not fixed
    -- columns) since the field set is LLM-extracted and open-ended
    -- (age/capital/horizon are core, but anything from dependents to
    -- past panic-selling behavior can show up depending on conversation).
    CREATE TABLE IF NOT EXISTS financial_profiles (
        user_id     INTEGER PRIMARY KEY REFERENCES users(id),
        profile     JSONB NOT NULL DEFAULT '{}',
        updated_at  TIMESTAMPTZ DEFAULT now()
    );

    -- Shared, tool-keyed evidence cache for the research graph (see
    -- graph/tool_catalogue.py, RFC_AGENTIC_RESEARCH_GRAPH.md Sec8). Not
    -- per-tenant — same reasoning as pdf_cache, a fundamental
    -- ratio for RELIANCE is the same fact regardless of which user asked.
    -- TTL enforcement is application-side (tools/data/evidence_cache.py)
    -- keyed by evidence_type, not a DB-level expiry.
    CREATE TABLE IF NOT EXISTS evidence_cache (
        id             SERIAL PRIMARY KEY,
        tool           TEXT NOT NULL,
        args_hash      TEXT NOT NULL,
        entity         TEXT NOT NULL,
        evidence_type  TEXT NOT NULL,
        payload        JSONB NOT NULL,
        confidence     REAL NOT NULL DEFAULT 1.0,
        fetched_at     TIMESTAMPTZ DEFAULT now(),
        UNIQUE (tool, args_hash)
    );
    CREATE INDEX IF NOT EXISTS idx_evidence_cache_entity ON evidence_cache (entity, evidence_type);
    -- DATA-01 purge (idempotent): rows that should never have been cached —
    -- write tools and user-scoped/query-dependent tools written before the
    -- cache key covered their inputs. Pre-fix rows for every other tool are
    -- orphaned by the key-version bump in tools/data/evidence_cache.py.
    DELETE FROM evidence_cache WHERE tool IN (
        'alert_create', 'alert_cancel', 'alert_pause', 'alert_resume', 'alert_list',
        'screener_save', 'screener_to_markdown'
    );

    -- Bring-your-own LLM key. One row per user; provider is a free-text
    -- tag ("openai_compatible" today) rather than an enum since new
    -- providers are meant to be addable without a migration.
    CREATE TABLE IF NOT EXISTS user_llm_config (
        user_id           INTEGER PRIMARY KEY REFERENCES users(id),
        provider          TEXT NOT NULL,
        base_url          TEXT,
        model             TEXT NOT NULL,
        api_key_encrypted TEXT NOT NULL,
        updated_at        TIMESTAMPTZ DEFAULT now()
    );

    -- Short-lived deep-link codes for the Telegram /start linking flow
    -- (utils/telegram_link.py). A code is single-use and expires quickly;
    -- old rows are harmless clutter, not cleaned up here.
    CREATE TABLE IF NOT EXISTS telegram_link_codes (
        code        TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        expires_at  TIMESTAMPTZ NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT now()
    );

    -- Postgres-backed signup/login rate limiting (utils/auth.py) —
    -- durable across restarts, unlike the in-memory dict it replaces.
    -- Old rows outside any window are inert (never matched by the
    -- time-bounded query in _check_rate_limit) and cheap to leave.
    CREATE TABLE IF NOT EXISTS auth_attempts (
        id           SERIAL PRIMARY KEY,
        key          TEXT NOT NULL,
        attempted_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_auth_attempts_key_time ON auth_attempts (key, attempted_at);
"""

# DATA-08 alert delivery tracking + the in-app notification inbox.
ALERT_DELIVERY_SQL = """
    ALTER TABLE alerts ADD COLUMN IF NOT EXISTS delivery_attempts   INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_delivery_error TEXT;
    ALTER TABLE alerts ADD COLUMN IF NOT EXISTS delivery_status     TEXT;
    CREATE TABLE IF NOT EXISTS notifications (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        alert_id    INTEGER REFERENCES alerts(id) ON DELETE SET NULL,
        message     TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT now(),
        read_at     TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications (user_id) WHERE read_at IS NULL;
"""

_TABLES = (
    "notifications",
    "auth_attempts",
    "telegram_link_codes",
    "user_llm_config",
    "evidence_cache",
    "financial_profiles",
    "pdf_cache",
    "reports",
    "saved_screeners",
    "chat_history",
    "chat_sessions",
    "alerts",
    "watchlist",
    "portfolio",
    "sessions",
    "users",
)


def upgrade() -> None:
    op.execute(SCHEMA_SQL)
    op.execute(ALERT_DELIVERY_SQL)


def downgrade() -> None:
    # Dropping the baseline deletes every table and all user data. Refuse
    # unless explicitly asked (tests, or a deliberate local reset).
    if os.getenv("ALEMBIC_ALLOW_DESTRUCTIVE") != "1":
        raise RuntimeError(
            "Downgrading the baseline drops all data; set ALEMBIC_ALLOW_DESTRUCTIVE=1 to confirm."
        )
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
