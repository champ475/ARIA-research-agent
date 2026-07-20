"""
utils/db.py — SQLite schema and query helpers.
Creates the DB and all tables on first run.
"""

import sqlite3
import json
import os
from datetime import datetime
from config import DB_PATH


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = get_conn()
    c = conn.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        email         TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at    TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        token       TEXT UNIQUE NOT NULL,
        expires_at  TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS portfolio (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        symbol      TEXT NOT NULL,
        qty         REAL NOT NULL,
        avg_price   REAL NOT NULL,
        added_at    TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS watchlist (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        symbol      TEXT NOT NULL,
        added_at    TEXT DEFAULT (datetime('now')),
        UNIQUE(user_id, symbol)
    );

    CREATE TABLE IF NOT EXISTS alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        symbol      TEXT NOT NULL,
        alert_type  TEXT NOT NULL,
        condition   TEXT NOT NULL,
        threshold   REAL,
        active      INTEGER DEFAULT 1,
        created_at  TEXT DEFAULT (datetime('now')),
        triggered_at TEXT
    );

    CREATE TABLE IF NOT EXISTS cache_meta (
        key         TEXT PRIMARY KEY,
        fetched_at  TEXT NOT NULL,
        ttl_seconds INTEGER DEFAULT 3600
    );

    CREATE TABLE IF NOT EXISTS chat_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        tab         TEXT NOT NULL,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        ts          TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS saved_screeners (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        name        TEXT NOT NULL,
        filters_json TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        symbol      TEXT,
        report_type TEXT NOT NULL,
        title       TEXT NOT NULL,
        content     TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    );

    -- pdf_cache is a shared, symbol-keyed fetch cache (not per-tenant data),
    -- so it intentionally has no user_id — same reasoning as cache_meta.
    CREATE TABLE IF NOT EXISTS pdf_cache (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT NOT NULL,
        doc_type    TEXT NOT NULL,
        period      TEXT,
        url         TEXT,
        text_content TEXT,
        fetched_at  TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    _migrate_add_user_id(conn)
    conn.commit()
    conn.close()


# Tables that gained a user_id column after the auth/multi-tenancy migration.
# CREATE TABLE IF NOT EXISTS above is a no-op on a pre-existing single-user
# SQLite file, so this backfills the column on old databases.
_USER_SCOPED_TABLES = ["portfolio", "watchlist", "alerts", "chat_history", "saved_screeners", "reports"]


def _migrate_add_user_id(conn: sqlite3.Connection):
    for table in _USER_SCOPED_TABLES:
        cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if cols and "user_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")


# ── Portfolio helpers ─────────────────────────────────────────────────────────

def portfolio_add(user_id: int, symbol: str, qty: float, avg_price: float):
    conn = get_conn()
    conn.execute(
        "INSERT INTO portfolio (user_id, symbol, qty, avg_price) VALUES (?,?,?,?)",
        (user_id, symbol.upper(), qty, avg_price),
    )
    conn.commit(); conn.close()


def portfolio_remove(user_id: int, symbol: str):
    conn = get_conn()
    conn.execute("DELETE FROM portfolio WHERE user_id=? AND symbol=?", (user_id, symbol.upper()))
    conn.commit(); conn.close()


def portfolio_all(user_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM portfolio WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Watchlist helpers ─────────────────────────────────────────────────────────

def watchlist_add(user_id: int, symbol: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (user_id, symbol) VALUES (?,?)",
        (user_id, symbol.upper()),
    )
    conn.commit(); conn.close()


def watchlist_remove(user_id: int, symbol: str):
    conn = get_conn()
    conn.execute("DELETE FROM watchlist WHERE user_id=? AND symbol=?", (user_id, symbol.upper()))
    conn.commit(); conn.close()


def watchlist_all(user_id: int) -> list[str]:
    conn = get_conn()
    rows = conn.execute("SELECT symbol FROM watchlist WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    return [r["symbol"] for r in rows]


# ── Chat history helpers ──────────────────────────────────────────────────────

def chat_save(user_id: int, tab: str, role: str, content: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO chat_history (user_id, tab, role, content) VALUES (?,?,?,?)",
        (user_id, tab, role, content),
    )
    conn.commit(); conn.close()


def chat_load(user_id: int, tab: str, limit: int = 50) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content FROM chat_history WHERE user_id=? AND tab=? ORDER BY ts DESC LIMIT ?",
        (user_id, tab, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


# ── Report helpers ────────────────────────────────────────────────────────────

def report_save(user_id: int, symbol: str, report_type: str, title: str, content: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO reports (user_id, symbol, report_type, title, content) VALUES (?,?,?,?,?)",
        (user_id, symbol, report_type, title, content),
    )
    rid = cur.lastrowid
    conn.commit(); conn.close()
    return rid


def report_list(user_id: int, limit: int = 20) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, symbol, report_type, title, created_at FROM reports "
        "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── PDF cache helpers ─────────────────────────────────────────────────────────

def pdf_cache_get(symbol: str, doc_type: str, period: str = None) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT text_content FROM pdf_cache WHERE symbol=? AND doc_type=? AND (period=? OR period IS NULL) LIMIT 1",
        (symbol.upper(), doc_type, period),
    ).fetchone()
    conn.close()
    return row["text_content"] if row else None


def pdf_cache_set(symbol: str, doc_type: str, text: str, period: str = None, url: str = None):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO pdf_cache (symbol, doc_type, period, url, text_content) VALUES (?,?,?,?,?)",
        (symbol.upper(), doc_type, period, url, text),
    )
    conn.commit(); conn.close()


# ── Saved screener helpers ────────────────────────────────────────────────────

def screener_save(user_id: int, name: str, filters: dict):
    conn = get_conn()
    conn.execute(
        "INSERT INTO saved_screeners (user_id, name, filters_json) VALUES (?,?,?)",
        (user_id, name, json.dumps(filters)),
    )
    conn.commit(); conn.close()


def screener_list(user_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name, filters_json, created_at FROM saved_screeners WHERE user_id=?",
        (user_id,),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "filters": json.loads(r["filters_json"]), "created_at": r["created_at"]} for r in rows]
