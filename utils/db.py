"""
utils/db.py — Postgres (Neon) schema and query helpers.
Single shared DB: read/written by the local Gradio app AND by
scripts/check_alerts.py (run on a schedule via GitHub Actions).
Creates the DB and all tables on first run.
"""

import json
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from config import DATABASE_URL

_pool: "pool.ThreadedConnectionPool | None" = None


def _get_pool() -> "pool.ThreadedConnectionPool":
    global _pool
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(
            minconn=1, maxconn=10, dsn=DATABASE_URL, cursor_factory=RealDictCursor
        )
    return _pool


def get_conn():
    """Returns a pooled connection. Caller MUST release it via release_conn()
    (not conn.close(), which would just drop the socket instead of returning
    it to the pool)."""
    return _get_pool().getconn()


def release_conn(conn):
    """Rolls back any uncommitted/aborted transaction before returning the
    connection to the pool — without this, a failed statement (e.g. a CHECK
    violation) leaves the connection stuck in an aborted-transaction state
    for whoever borrows it next."""
    try:
        if not conn.closed:
            conn.rollback()
    finally:
        _get_pool().putconn(conn)


def _query(sql: str, params: tuple = (), fetch: str | None = "all", commit: bool = False):
    """fetch: 'all' | 'one' | None. Returns list[dict] | dict|None | None."""
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
        return result
    finally:
        release_conn(conn)


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
            triggered_at  TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts (active) WHERE active = TRUE;

        CREATE TABLE IF NOT EXISTS cache_meta (
            key         TEXT PRIMARY KEY,
            fetched_at  TIMESTAMPTZ NOT NULL,
            ttl_seconds INTEGER DEFAULT 3600
        );

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
        -- so it intentionally has no user_id — same reasoning as cache_meta.
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
        """)
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


def session_touch(session_id: int, title: str | None = None):
    if title is not None:
        _query(
            "UPDATE chat_sessions SET updated_at=now(), title=%s WHERE id=%s",
            (title, session_id), fetch=None, commit=True,
        )
    else:
        _query(
            "UPDATE chat_sessions SET updated_at=now() WHERE id=%s",
            (session_id,), fetch=None, commit=True,
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
    _query(
        "INSERT INTO chat_history (user_id, session_id, tab, role, content) VALUES (%s,%s,%s,%s,%s)",
        (user_id, session_id, tab, role, content), fetch=None, commit=True,
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

def alert_add(user_id: int, symbol: str, alert_type: str, condition: str, threshold: float | None) -> int:
    row = _query(
        "INSERT INTO alerts (user_id, symbol, alert_type, condition, threshold) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (user_id, symbol.upper(), alert_type, condition, threshold),
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


def alert_mark_triggered(alert_id: int):
    """Called only by scripts/check_alerts.py — fires once, then deactivates."""
    _query(
        "UPDATE alerts SET active=FALSE, triggered_at=now() WHERE id=%s",
        (alert_id,), fetch=None, commit=True,
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


def profile_clear(user_id: int):
    _query("DELETE FROM financial_profiles WHERE user_id=%s", (user_id,), fetch=None, commit=True)
