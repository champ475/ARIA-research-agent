"""
utils/db.py — Postgres (Neon) schema and query helpers.
Single shared DB: read/written by the local Gradio app AND by
scripts/check_alerts.py (run on a schedule via GitHub Actions).
Creates the DB and all tables on first run.
"""

import json
import os
import threading
import logging
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from config import DATABASE_URL

log = logging.getLogger(__name__)

_pool: "pool.ThreadedConnectionPool | None" = None

# REL-01: psycopg2's ThreadedConnectionPool *raises* PoolError once maxconn
# connections are checked out, instead of waiting. With concurrent chat turns
# each fanning tools out in parallel, that turned a busy moment into failed
# turns. A semaphore sized to the pool makes callers wait (up to
# DB_POOL_ACQUIRE_TIMEOUT seconds) for a free connection instead.
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "20"))
DB_POOL_ACQUIRE_TIMEOUT = float(os.getenv("DB_POOL_ACQUIRE_TIMEOUT", "10"))
_pool_slots = threading.BoundedSemaphore(DB_POOL_MAX)


class PoolTimeout(psycopg2.OperationalError):
    """No pooled connection became free within DB_POOL_ACQUIRE_TIMEOUT."""


def _get_pool() -> "pool.ThreadedConnectionPool":
    global _pool
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(
            minconn=1, maxconn=DB_POOL_MAX, dsn=DATABASE_URL, cursor_factory=RealDictCursor
        )
    return _pool


def get_conn():
    """Returns a pooled connection, waiting for a free one if the pool is
    fully checked out. Caller MUST release it via release_conn() (not
    conn.close(), which would just drop the socket instead of returning it to
    the pool, and would leak a pool slot)."""
    if not _pool_slots.acquire(timeout=DB_POOL_ACQUIRE_TIMEOUT):
        raise PoolTimeout(f"no database connection free within {DB_POOL_ACQUIRE_TIMEOUT:.0f}s")
    try:
        return _get_pool().getconn()
    except BaseException:
        _pool_slots.release()
        raise


def release_conn(conn, discard: bool = False):
    """Rolls back any uncommitted/aborted transaction before returning the
    connection to the pool — without this, a failed statement (e.g. a CHECK
    violation) leaves the connection stuck in an aborted-transaction state
    for whoever borrows it next.

    `discard=True` closes the connection and drops it from the pool instead
    of returning it — for a connection Neon has already killed server-side
    (`OperationalError: server closed the connection unexpectedly`, common
    on a serverless/autosuspend Postgres tier after a period of inactivity).
    Putting a dead connection back would just hand the same failure to the
    next caller."""
    try:
        if discard:
            try:
                conn.close()
            except Exception:
                pass
            _get_pool().putconn(conn, close=True)
            return
        if not conn.closed:
            conn.rollback()
    finally:
        try:
            if not discard:
                _get_pool().putconn(conn)
        finally:
            _pool_slots.release()


def _query(sql: str, params: tuple = (), fetch: str | None = "all", commit: bool = False):
    """fetch: 'all' | 'one' | None. Returns list[dict] | dict|None | None.

    A pooled connection Neon has already dropped server-side (idle timeout on
    the free/autosuspend tier) raises OperationalError/InterfaceError on
    first use, not at checkout — the pool has no way to know it's dead until
    something tries to use it. One retry on a fresh connection self-heals
    that common case instead of surfacing it as a hard failure for whatever
    the user happened to be doing (this is exactly what crashed a chat
    turn's post-processing DB save mid-audit, leaving the input locked)."""
    for attempt in range(2):
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            result = None
            if fetch == "all":
                result = [dict(r) for r in cur.fetchall()]
            elif fetch == "one":
                row = cur.fetchone()
                result = dict(row) if row else None
            if commit:
                conn.commit()
            release_conn(conn)
            return result
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            release_conn(conn, discard=True)
            if attempt == 1:
                raise
            log.warning(f"[db] stale pooled connection ({e}); retrying once with a fresh one")
        except Exception:
            release_conn(conn)
            raise


# DATA-08: alert delivery tracking + the in-app notification inbox. Kept as a
# standalone constant because scripts/check_alerts.py (a separate GitHub
# Actions process that never runs init_db) applies it too. Idempotent.
ALERT_DELIVERY_SCHEMA_SQL = """
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


def init_db():
    """Create all tables if they don't exist."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
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
        """)
        cur.execute(ALERT_DELIVERY_SCHEMA_SQL)
        conn.commit()
    finally:
        release_conn(conn)


# ── Portfolio helpers ─────────────────────────────────────────────────────────

def portfolio_add(user_id: int, symbol: str, qty: float, avg_price: float):
    _query(
        "INSERT INTO portfolio (user_id, symbol, qty, avg_price) VALUES (%s,%s,%s,%s)",
        (user_id, symbol.upper(), qty, avg_price),
        fetch=None, commit=True,
    )


def portfolio_remove(user_id: int, symbol: str):
    _query(
        "DELETE FROM portfolio WHERE user_id=%s AND symbol=%s",
        (user_id, symbol.upper()), fetch=None, commit=True,
    )


def portfolio_all(user_id: int) -> list[dict]:
    return _query("SELECT * FROM portfolio WHERE user_id=%s", (user_id,), fetch="all")


# ── Watchlist helpers ─────────────────────────────────────────────────────────

def watchlist_add(user_id: int, symbol: str):
    _query(
        "INSERT INTO watchlist (user_id, symbol) VALUES (%s,%s) "
        "ON CONFLICT (user_id, symbol) DO NOTHING",
        (user_id, symbol.upper()), fetch=None, commit=True,
    )


def watchlist_remove(user_id: int, symbol: str):
    _query(
        "DELETE FROM watchlist WHERE user_id=%s AND symbol=%s",
        (user_id, symbol.upper()), fetch=None, commit=True,
    )


def watchlist_all(user_id: int) -> list[str]:
    rows = _query("SELECT symbol FROM watchlist WHERE user_id=%s", (user_id,), fetch="all")
    return [r["symbol"] for r in rows]


# ── Chat session helpers (ChatGPT-style sidebar history) ──────────────────────
# Each session is one conversation thread, scoped to a user + research tab.
# Sidebar lists sessions (newest first); chat_history rows hang off a session.

def session_create(user_id: int, tab: str, title: str = "New chat") -> int:
    row = _query(
        "INSERT INTO chat_sessions (user_id, tab, title) VALUES (%s,%s,%s) RETURNING id",
        (user_id, tab, title), fetch="one", commit=True,
    )
    return row["id"]


def session_list(user_id: int, tab: str, limit: int = 50) -> list[dict]:
    return _query(
        "SELECT id, title, updated_at FROM chat_sessions "
        "WHERE user_id=%s AND tab=%s ORDER BY updated_at DESC LIMIT %s",
        (user_id, tab, limit), fetch="all",
    )


def session_touch(user_id: int, session_id: int, title: str | None = None):
    """Bumps (and optionally retitles) a session — only if `user_id` owns it
    (SEC-13: previously any session id could be touched)."""
    if title is not None:
        _query(
            "UPDATE chat_sessions SET updated_at=now(), title=%s WHERE id=%s AND user_id=%s",
            (title, session_id, user_id), fetch=None, commit=True,
        )
    else:
        _query(
            "UPDATE chat_sessions SET updated_at=now() WHERE id=%s AND user_id=%s",
            (session_id, user_id), fetch=None, commit=True,
        )


def session_delete(user_id: int, session_id: int):
    """Deletes a session and its messages — only if it belongs to user_id."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM chat_sessions WHERE id=%s AND user_id=%s", (session_id, user_id)
        )
        owned = cur.fetchone()
        if owned:
            cur.execute("DELETE FROM chat_history WHERE session_id=%s", (session_id,))
            cur.execute("DELETE FROM chat_sessions WHERE id=%s", (session_id,))
            conn.commit()
    finally:
        release_conn(conn)


def chat_save(user_id: int, session_id: int, tab: str, role: str, content: str):
    """Appends a message to a session — only if `user_id` owns that session
    (SEC-13: a forged session id can't write into someone else's history)."""
    _query(
        "INSERT INTO chat_history (user_id, session_id, tab, role, content) "
        "SELECT %s, %s, %s, %s, %s "
        "WHERE EXISTS (SELECT 1 FROM chat_sessions WHERE id=%s AND user_id=%s)",
        (user_id, session_id, tab, role, content, session_id, user_id), fetch=None, commit=True,
    )


def chat_load(user_id: int, session_id: int) -> list[dict]:
    """Loads a session's messages — only if it belongs to user_id (tenant isolation)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM chat_sessions WHERE id=%s AND user_id=%s", (session_id, user_id)
        )
        if not cur.fetchone():
            return []
        cur.execute(
            "SELECT role, content FROM chat_history WHERE session_id=%s ORDER BY ts ASC",
            (session_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        release_conn(conn)


# ── Report helpers ────────────────────────────────────────────────────────────

def report_save(user_id: int, symbol: str, report_type: str, title: str, content: str) -> int:
    row = _query(
        "INSERT INTO reports (user_id, symbol, report_type, title, content) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (user_id, symbol, report_type, title, content), fetch="one", commit=True,
    )
    return row["id"]


def report_list(user_id: int, limit: int = 20) -> list[dict]:
    return _query(
        "SELECT id, symbol, report_type, title, created_at FROM reports "
        "WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (user_id, limit), fetch="all",
    )


# ── PDF cache helpers ─────────────────────────────────────────────────────────

def pdf_cache_get(symbol: str, doc_type: str, period: str = None) -> str | None:
    row = _query(
        "SELECT text_content FROM pdf_cache WHERE symbol=%s AND doc_type=%s AND (period=%s OR period IS NULL) LIMIT 1",
        (symbol.upper(), doc_type, period), fetch="one",
    )
    return row["text_content"] if row else None


def pdf_cache_set(symbol: str, doc_type: str, text: str, period: str = None, url: str = None):
    _query(
        "INSERT INTO pdf_cache (symbol, doc_type, period, url, text_content) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (symbol, doc_type, period) DO UPDATE SET "
        "url=EXCLUDED.url, text_content=EXCLUDED.text_content, fetched_at=now()",
        (symbol.upper(), doc_type, period, url, text), fetch=None, commit=True,
    )


# ── Saved screener helpers ────────────────────────────────────────────────────

def screener_save(user_id: int, name: str, filters: dict):
    _query(
        "INSERT INTO saved_screeners (user_id, name, filters_json) VALUES (%s,%s,%s)",
        (user_id, name, json.dumps(filters)), fetch=None, commit=True,
    )


def screener_list(user_id: int) -> list[dict]:
    rows = _query(
        "SELECT id, name, filters_json, created_at FROM saved_screeners WHERE user_id=%s",
        (user_id,), fetch="all",
    )
    return [{"id": r["id"], "name": r["name"], "filters": json.loads(r["filters_json"]), "created_at": r["created_at"]} for r in rows]


# ── Alert helpers ─────────────────────────────────────────────────────────────

def alert_add(user_id: int, symbol: str, alert_type: str, condition: str, threshold: float | None,
              recurring: bool = False, cooldown_hours: int = 24) -> int:
    row = _query(
        "INSERT INTO alerts (user_id, symbol, alert_type, condition, threshold, recurring, cooldown_hours) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (user_id, symbol.upper(), alert_type, condition, threshold, recurring, cooldown_hours),
        fetch="one", commit=True,
    )
    return row["id"]


def alert_list(user_id: int, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM alerts WHERE user_id=%s"
    if active_only:
        sql += " AND active=TRUE"
    sql += " ORDER BY created_at DESC"
    return _query(sql, (user_id,), fetch="all")


def alert_remove(user_id: int, alert_id: int) -> bool:
    """Deletes only if owned by user_id (tenant isolation, mirrors session_delete)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM alerts WHERE id=%s AND user_id=%s", (alert_id, user_id))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        release_conn(conn)


def alert_pause(user_id: int, alert_id: int):
    _query(
        "UPDATE alerts SET active=FALSE WHERE id=%s AND user_id=%s",
        (alert_id, user_id), fetch=None, commit=True,
    )


def alert_resume(user_id: int, alert_id: int):
    _query(
        "UPDATE alerts SET active=TRUE WHERE id=%s AND user_id=%s",
        (alert_id, user_id), fetch=None, commit=True,
    )


def alert_mark_triggered(alert_id: int, recurring: bool = False):
    """Called only by scripts/check_alerts.py. One-shot alerts deactivate on
    fire; recurring alerts stay active — check_alerts.py's cooldown_hours
    check prevents them from re-firing on every 5-min cron tick."""
    if recurring:
        _query(
            "UPDATE alerts SET triggered_at=now() WHERE id=%s",
            (alert_id,), fetch=None, commit=True,
        )
    else:
        _query(
            "UPDATE alerts SET active=FALSE, triggered_at=now() WHERE id=%s",
            (alert_id,), fetch=None, commit=True,
        )


# ── In-app notification inbox (DATA-08) ───────────────────────────────────────
# The fallback delivery channel for alerts: written by scripts/check_alerts.py
# when Telegram isn't linked or keeps failing, read in the Settings tab.

def notification_add(user_id: int, message: str, alert_id: int | None = None) -> int:
    row = _query(
        "INSERT INTO notifications (user_id, alert_id, message) VALUES (%s, %s, %s) RETURNING id",
        (user_id, alert_id, message), fetch="one", commit=True,
    )
    return row["id"]


def notifications_list(user_id: int, unread_only: bool = False, limit: int = 50) -> list[dict]:
    sql = "SELECT id, alert_id, message, created_at, read_at FROM notifications WHERE user_id=%s"
    if unread_only:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY created_at DESC, id DESC LIMIT %s"
    return _query(sql, (user_id, limit))


def notifications_unread_count(user_id: int) -> int:
    row = _query(
        "SELECT count(*) AS n FROM notifications WHERE user_id=%s AND read_at IS NULL", (user_id,), fetch="one"
    )
    return int(row["n"]) if row else 0


def notifications_mark_read(user_id: int, ids: list[int] | None = None) -> None:
    """Marks the given notifications (or all unread ones) read — only the
    caller's own rows, whatever ids are passed."""
    if ids is None:
        _query(
            "UPDATE notifications SET read_at=now() WHERE user_id=%s AND read_at IS NULL",
            (user_id,), fetch=None, commit=True,
        )
    elif ids:
        _query(
            "UPDATE notifications SET read_at=now() WHERE user_id=%s AND id = ANY(%s) AND read_at IS NULL",
            (user_id, list(ids)), fetch=None, commit=True,
        )


def alert_get_active_all() -> list[dict]:
    """Cross-tenant fetch — used only by the standalone checker script, never
    by the Gradio app (which is always user_id-scoped)."""
    return _query("SELECT * FROM alerts WHERE active=TRUE ORDER BY id", fetch="all")


# ── Financial profile helpers (PlanningAgent) ─────────────────────────────────

def profile_get(user_id: int) -> dict:
    row = _query("SELECT profile FROM financial_profiles WHERE user_id=%s", (user_id,), fetch="one")
    return row["profile"] if row else {}


def profile_upsert(user_id: int, updates: dict) -> dict:
    """Merges `updates` into the stored profile (shallow merge — new keys
    overwrite old ones, existing keys not present in `updates` are kept) and
    returns the resulting full profile."""
    current = profile_get(user_id)
    current.update(updates)
    _query(
        "INSERT INTO financial_profiles (user_id, profile, updated_at) VALUES (%s,%s,now()) "
        "ON CONFLICT (user_id) DO UPDATE SET profile=%s, updated_at=now()",
        (user_id, json.dumps(current), json.dumps(current)), fetch=None, commit=True,
    )
    return current


# ── Evidence cache helpers (research graph, see tools/data/evidence_cache.py) ─

def evidence_cache_get(tool: str, args_hash: str) -> dict | None:
    return _query(
        "SELECT payload, confidence, fetched_at FROM evidence_cache WHERE tool=%s AND args_hash=%s",
        (tool, args_hash), fetch="one",
    )


def evidence_cache_put(tool: str, args_hash: str, entity: str, evidence_type: str, payload: dict, confidence: float = 1.0) -> None:
    _query(
        "INSERT INTO evidence_cache (tool, args_hash, entity, evidence_type, payload, confidence) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (tool, args_hash) DO UPDATE "
        "SET payload=EXCLUDED.payload, confidence=EXCLUDED.confidence, fetched_at=now()",
        (tool, args_hash, entity, evidence_type, json.dumps(payload, default=str), confidence),
        fetch=None, commit=True,
    )


def profile_clear(user_id: int):
    _query("DELETE FROM financial_profiles WHERE user_id=%s", (user_id,), fetch=None, commit=True)


# ── LLM config helpers (bring-your-own-key, see utils/crypto.py) ─────────────

def llm_config_get(user_id: int) -> dict | None:
    return _query(
        "SELECT provider, base_url, model, api_key_encrypted FROM user_llm_config WHERE user_id=%s",
        (user_id,), fetch="one",
    )


def llm_config_set(user_id: int, provider: str, model: str, api_key_encrypted: str, base_url: str | None = None):
    _query(
        "INSERT INTO user_llm_config (user_id, provider, base_url, model, api_key_encrypted, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,now()) "
        "ON CONFLICT (user_id) DO UPDATE SET provider=%s, base_url=%s, model=%s, api_key_encrypted=%s, updated_at=now()",
        (user_id, provider, base_url, model, api_key_encrypted, provider, base_url, model, api_key_encrypted),
        fetch=None, commit=True,
    )


def llm_config_clear(user_id: int):
    _query("DELETE FROM user_llm_config WHERE user_id=%s", (user_id,), fetch=None, commit=True)


# ── Free-trial quota helpers (agents/graph_orchestrator.py) ──────────────────

def trial_quota_check_and_increment(user_id: int, daily_limit: int, today: "date") -> bool:
    """Atomically checks-and-increments a user's free-trial message count for
    `today` (an IST calendar date, passed in rather than computed here so
    every caller uses the same clock read). Returns True if this message is
    allowed. Resets the counter the first time a new day is seen — no
    separate cron/cleanup job needed, the reset just happens lazily on next
    use, same pattern as the alert cooldown check in scripts/check_alerts.py."""
    row = _query(
        "SELECT free_msgs_used_today, free_msgs_reset_at FROM users WHERE id=%s",
        (user_id,), fetch="one",
    )
    if row is None:
        return False
    used = row["free_msgs_used_today"]
    reset_at = row["free_msgs_reset_at"]
    if reset_at != today:
        used = 0
    if used >= daily_limit:
        _query(
            "UPDATE users SET free_msgs_used_today=%s, free_msgs_reset_at=%s WHERE id=%s",
            (used, today, user_id), fetch=None, commit=True,
        )
        return False
    _query(
        "UPDATE users SET free_msgs_used_today=%s, free_msgs_reset_at=%s WHERE id=%s",
        (used + 1, today, user_id), fetch=None, commit=True,
    )
    return True


# ── Telegram linking helpers (utils/telegram_link.py) ─────────────────────────

def telegram_link_code_create(user_id: int, code: str, expires_at) -> None:
    _query(
        "INSERT INTO telegram_link_codes (code, user_id, expires_at) VALUES (%s,%s,%s)",
        (code, user_id, expires_at), fetch=None, commit=True,
    )


def telegram_link_code_resolve(code: str):
    """Returns the owning user_id if `code` exists and hasn't expired, else
    None. Deletes the code either way (single-use) so a stale/guessed code
    can't be retried."""
    row = _query(
        "SELECT user_id, expires_at FROM telegram_link_codes WHERE code=%s",
        (code,), fetch="one",
    )
    _query("DELETE FROM telegram_link_codes WHERE code=%s", (code,), fetch=None, commit=True)
    if row is None:
        return None
    from datetime import datetime, timezone
    if row["expires_at"] < datetime.now(timezone.utc):
        return None
    return row["user_id"]


def telegram_chat_id_set(user_id: int, chat_id: str) -> None:
    _query("UPDATE users SET telegram_chat_id=%s WHERE id=%s", (chat_id, user_id), fetch=None, commit=True)


def telegram_chat_id_get(user_id: int) -> str | None:
    row = _query("SELECT telegram_chat_id FROM users WHERE id=%s", (user_id,), fetch="one")
    return row["telegram_chat_id"] if row else None


# ── Terms-of-service consent (utils/auth.py) ──────────────────────────────────

def accept_terms(user_id: int) -> None:
    _query("UPDATE users SET accepted_terms_at=now() WHERE id=%s", (user_id,), fetch=None, commit=True)


# ── Postgres-backed auth rate limiting (replaces utils/auth.py's in-memory dict) ─

def auth_attempt_record(key: str) -> None:
    _query("INSERT INTO auth_attempts (key) VALUES (%s)", (key,), fetch=None, commit=True)


def auth_attempt_count(key: str, since) -> int:
    row = _query(
        "SELECT COUNT(*) AS n FROM auth_attempts WHERE key=%s AND attempted_at >= %s",
        (key, since), fetch="one",
    )
    return row["n"] if row else 0


def auth_attempt_clear(key: str) -> None:
    _query("DELETE FROM auth_attempts WHERE key=%s", (key,), fetch=None, commit=True)


# ── Report content helper (regenerate-on-demand downloads, see app.py) ───────

def report_get(user_id: int, report_id: int) -> dict | None:
    """Fetches one report's full content — only if owned by user_id (tenant
    isolation, mirrors session_delete/alert_remove's ownership-check pattern)."""
    return _query(
        "SELECT id, symbol, report_type, title, content, created_at FROM reports WHERE id=%s AND user_id=%s",
        (report_id, user_id), fetch="one",
    )
