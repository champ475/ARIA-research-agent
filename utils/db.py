"""
utils/db.py — Postgres (Neon) query helpers.
Single shared DB: read/written by the local Gradio app AND by
scripts/check_alerts.py (run on a schedule via GitHub Actions).
The schema itself lives in Alembic migrations (migrations/versions/, ARCH-01).
"""

import contextlib
import json
import logging
import threading
from datetime import UTC, date
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg_pool import PoolTimeout as _PoolTimeout

from config import (
    DATABASE_URL,
    DB_CONNECT_TIMEOUT,
    DB_POOL_ACQUIRE_TIMEOUT,
    DB_POOL_MAX,
    DB_POOL_MIN,
    DB_STATEMENT_TIMEOUT_MS,
    db_host,
)

log = logging.getLogger(__name__)

# REL-04: one Postgres driver (psycopg 3) and one waiting pool, sized by
# DB_POOL_MAX. getconn() *waits* up to DB_POOL_ACQUIRE_TIMEOUT for a free
# connection (psycopg2's pool raised PoolError immediately, which REL-01's
# semaphore used to paper over), then raises PoolTimeout.
_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

# psycopg_pool's own timeout error — callers catch utils.db.PoolTimeout.
PoolTimeout = _PoolTimeout

# Driver exceptions callers catch, re-exported so nothing outside this module
# needs to import the driver.
OperationalError = psycopg.OperationalError
InterfaceError = psycopg.InterfaceError


def _behind_neon_pooler(url: str) -> bool:
    """Neon's PgBouncer endpoint ("ep-...-pooler.") refuses `options` at connect."""
    return "-pooler." in db_host(url)


def connection_kwargs() -> dict[str, Any]:
    """Settings every app connection is opened with (REL-04).

    - dict rows, like psycopg2's RealDictCursor;
    - `statement_timeout` so a runaway query is cancelled server-side instead
      of pinning a pooled connection (0 disables it). Neon's `-pooler` host
      rejects it as a startup option, so it is skipped there and set per role
      instead (`ALTER ROLE ... SET statement_timeout`, docs/RUNBOOK.md);
    - prepared statements off (`prepare_threshold=None`): Neon's `-pooler`
      endpoint is PgBouncer in transaction mode, where server-side prepared
      statements break;
    - an application name so `pg_stat_activity` shows who holds a connection.
    """
    kwargs: dict[str, Any] = {
        "row_factory": dict_row,
        "prepare_threshold": None,
        "connect_timeout": DB_CONNECT_TIMEOUT,
        "application_name": "aria",
    }
    if DB_STATEMENT_TIMEOUT_MS > 0 and not _behind_neon_pooler(DATABASE_URL):
        kwargs["options"] = f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}"
    return kwargs


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    DATABASE_URL,
                    min_size=min(DB_POOL_MIN, DB_POOL_MAX),
                    max_size=DB_POOL_MAX,
                    timeout=DB_POOL_ACQUIRE_TIMEOUT,
                    kwargs=connection_kwargs(),
                    # Neon drops idle connections server-side; check each one
                    # as it is handed out instead of failing the caller's query.
                    check=ConnectionPool.check_connection,
                    name="aria",
                    open=True,
                )
    return _pool


def close_pool() -> None:
    """Close every pooled connection (graceful shutdown, tests)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def pool_stats() -> dict[str, int]:
    """Pool usage for metrics / readiness (OBS-03): size, in use, waiting."""
    if _pool is None:
        return {"pool_size": 0, "pool_available": 0, "requests_waiting": 0, "pool_max": DB_POOL_MAX}
    st = _pool.get_stats()
    return {
        "pool_size": st.get("pool_size", 0),
        "pool_available": st.get("pool_available", 0),
        "requests_waiting": st.get("requests_waiting", 0),
        "pool_max": DB_POOL_MAX,
    }


def get_conn() -> psycopg.Connection:
    """Returns a pooled connection, waiting for a free one if the pool is
    fully checked out (PoolTimeout after DB_POOL_ACQUIRE_TIMEOUT). Caller
    MUST release it via release_conn()."""
    return _get_pool().getconn(timeout=DB_POOL_ACQUIRE_TIMEOUT)


def release_conn(conn: psycopg.Connection, discard: bool = False) -> None:
    """Rolls back any uncommitted/aborted transaction before returning the
    connection to the pool — without this, a failed statement (e.g. a CHECK
    violation) leaves the connection stuck in an aborted-transaction state
    for whoever borrows it next.

    `discard=True` closes the connection first; the pool then drops it and
    opens a replacement — for a connection Neon has already killed
    server-side (`server closed the connection unexpectedly`)."""
    if discard:
        with contextlib.suppress(Exception):
            conn.close()
    elif not conn.closed:
        with contextlib.suppress(Exception):
            conn.rollback()
    _get_pool().putconn(conn)


def _query(sql: str, params: tuple | dict = (), fetch: str | None = "all", commit: bool = False):
    """fetch: 'all' | 'one' | None. Returns list[dict] | dict|None | None.

    A pooled connection Neon has already dropped server-side can still fail
    on first use despite the checkout check (the race is tiny but real). One
    retry on a fresh connection self-heals that case instead of surfacing it
    as a hard failure for whatever the user happened to be doing."""
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
        except pg_errors.QueryCanceled:
            # statement_timeout fired: retrying would just time out again.
            release_conn(conn)
            raise
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            release_conn(conn, discard=True)
            if attempt == 1:
                raise
            log.warning("[db] stale pooled connection (%s); retrying once with a fresh one", type(e).__name__)
        except Exception:
            release_conn(conn)
            raise


def ensure_schema() -> None:
    """Startup check (ARCH-01): the app no longer creates or alters tables.
    Schema lives in Alembic revisions (migrations/versions/), applied by
    `python scripts/migrate.py` as a release step. Raises
    utils.migrations.SchemaOutOfDate if the database isn't at head, so the
    app fails at boot with an instruction instead of on a missing column
    mid-turn. RUN_MIGRATIONS_ON_START=true migrates first (single-instance
    deployments only — never with several replicas starting at once)."""
    from utils import migrations

    if migrations.run_migrations_on_start_enabled():
        log.warning("[db] RUN_MIGRATIONS_ON_START is set — applying migrations in-process")
        migrations.upgrade(DATABASE_URL)
        migrations.setup_checkpointer_tables(DATABASE_URL)
    conn = get_conn()
    try:
        migrations.check_schema(conn)
    finally:
        release_conn(conn)


# ── Portfolio helpers (UX-05) ────────────────────────────────────────────────
# One row per lot; qty and avg_price are NUMERIC (Decimal in Python).

MAX_PORTFOLIO_LOTS = 500


class PortfolioFull(Exception):
    """The user already holds MAX_PORTFOLIO_LOTS lots."""


def _lot_count(cur, user_id: int) -> int:
    # Serialise concurrent adds for one user so the cap can't be raced past.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('portfolio'), %s)", (user_id,))
    cur.execute("SELECT count(*) AS n FROM portfolio WHERE user_id=%s", (user_id,))
    return int(cur.fetchone()["n"])


def portfolio_add(user_id: int, symbol: str, qty, avg_price, buy_date=None) -> int:
    """Add one lot; returns its id. Raises PortfolioFull at the cap."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        if _lot_count(cur, user_id) >= MAX_PORTFOLIO_LOTS:
            raise PortfolioFull()
        cur.execute(
            "INSERT INTO portfolio (user_id, symbol, qty, avg_price, buy_date) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (user_id, symbol.upper(), qty, avg_price, buy_date),
        )
        lot_id = int(cur.fetchone()["id"])
        conn.commit()
        return lot_id
    finally:
        release_conn(conn)


def portfolio_update(user_id: int, lot_id: int, qty, avg_price, buy_date=None) -> bool:
    """Edit one of the user's lots; False if it isn't theirs / doesn't exist."""
    row = _query(
        "UPDATE portfolio SET qty=%s, avg_price=%s, buy_date=%s WHERE id=%s AND user_id=%s RETURNING id",
        (qty, avg_price, buy_date, lot_id, user_id),
        fetch="one",
        commit=True,
    )
    return row is not None


def portfolio_remove(user_id: int, symbol: str) -> int:
    """Remove every lot of `symbol`; returns how many."""
    rows = _query(
        "DELETE FROM portfolio WHERE user_id=%s AND symbol=%s RETURNING id",
        (user_id, symbol.upper()),
        commit=True,
    )
    return len(rows or [])


def portfolio_remove_lot(user_id: int, lot_id: int) -> bool:
    row = _query(
        "DELETE FROM portfolio WHERE id=%s AND user_id=%s RETURNING id",
        (lot_id, user_id),
        fetch="one",
        commit=True,
    )
    return row is not None


def portfolio_all(user_id: int) -> list[dict]:
    return _query(
        "SELECT id, symbol, qty, avg_price, buy_date, added_at FROM portfolio WHERE user_id=%s ORDER BY symbol, id",
        (user_id,),
        fetch="all",
    )


def portfolio_import(user_id: int, lots: list[dict], replace: bool = False) -> int:
    """Add (or, with replace, swap in) many lots in one transaction — all or
    nothing. Each lot: {symbol, qty, avg_price, buy_date?}."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        existing = _lot_count(cur, user_id)
        if replace:
            cur.execute("DELETE FROM portfolio WHERE user_id=%s", (user_id,))
            existing = 0
        if existing + len(lots) > MAX_PORTFOLIO_LOTS:
            raise PortfolioFull()
        cur.executemany(
            "INSERT INTO portfolio (user_id, symbol, qty, avg_price, buy_date) VALUES (%s,%s,%s,%s,%s)",
            [(user_id, x["symbol"].upper(), x["qty"], x["avg_price"], x.get("buy_date")) for x in lots],
        )
        conn.commit()
        return len(lots)
    finally:
        release_conn(conn)


# ── Watchlist helpers ─────────────────────────────────────────────────────────


class WatchlistFull(Exception):
    """The user already has WATCHLIST_MAX symbols (VAL-07)."""


def watchlist_add(user_id: int, symbol: str, max_symbols: int = 50) -> bool:
    """Add a symbol; False if it was already there. Raises WatchlistFull at
    the cap — checked inside the INSERT, so concurrent adds can't overshoot
    by more than their own concurrency."""
    symbol = symbol.upper()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM watchlist WHERE user_id=%s AND symbol=%s", (user_id, symbol))
        if cur.fetchone():
            return False
        cur.execute(
            "INSERT INTO watchlist (user_id, symbol) SELECT %s,%s "
            "WHERE (SELECT count(*) FROM watchlist WHERE user_id=%s) < %s "
            "ON CONFLICT (user_id, symbol) DO NOTHING RETURNING id",
            (user_id, symbol, user_id, max_symbols),
        )
        added = cur.fetchone() is not None
        conn.commit()
    finally:
        release_conn(conn)
    if not added:
        raise WatchlistFull(f"watchlist already has {max_symbols} symbols")
    return True


def watchlist_remove(user_id: int, symbol: str):
    _query(
        "DELETE FROM watchlist WHERE user_id=%s AND symbol=%s",
        (user_id, symbol.upper()),
        fetch=None,
        commit=True,
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
        (user_id, tab, title),
        fetch="one",
        commit=True,
    )
    return row["id"]


def session_list(user_id: int, tab: str, limit: int = 50) -> list[dict]:
    return _query(
        "SELECT id, title, updated_at FROM chat_sessions "
        "WHERE user_id=%s AND tab=%s ORDER BY updated_at DESC LIMIT %s",
        (user_id, tab, limit),
        fetch="all",
    )


def session_pending_get(user_id: int, session_id: int) -> str | None:
    """The LangGraph thread this chat is paused on for a yes/no (VAL-10), if any."""
    row = _query(
        "SELECT pending_thread_id FROM chat_sessions WHERE id=%s AND user_id=%s",
        (session_id, user_id),
        fetch="one",
    )
    return row["pending_thread_id"] if row else None


def session_pending_set(user_id: int, session_id: int, thread_id: str | None) -> None:
    """Record (or clear, with None) the paused thread — only on the user's own session."""
    _query(
        "UPDATE chat_sessions SET pending_thread_id=%s WHERE id=%s AND user_id=%s",
        (thread_id, session_id, user_id),
        fetch=None,
        commit=True,
    )


def session_touch(user_id: int, session_id: int, title: str | None = None):
    """Bumps (and optionally retitles) a session — only if `user_id` owns it
    (SEC-13: previously any session id could be touched)."""
    if title is not None:
        _query(
            "UPDATE chat_sessions SET updated_at=now(), title=%s WHERE id=%s AND user_id=%s",
            (title, session_id, user_id),
            fetch=None,
            commit=True,
        )
    else:
        _query(
            "UPDATE chat_sessions SET updated_at=now() WHERE id=%s AND user_id=%s",
            (session_id, user_id),
            fetch=None,
            commit=True,
        )


def session_delete(user_id: int, session_id: int):
    """Deletes a session and its messages — only if it belongs to user_id."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM chat_sessions WHERE id=%s AND user_id=%s", (session_id, user_id))
        owned = cur.fetchone()
        if owned:
            cur.execute("DELETE FROM chat_history WHERE session_id=%s", (session_id,))
            cur.execute("DELETE FROM chat_sessions WHERE id=%s", (session_id,))
            conn.commit()
    finally:
        release_conn(conn)


def chat_save(user_id: int, session_id: int, tab: str, role: str, content: str, meta: dict | None = None):
    """Appends a message to a session — only if `user_id` owns that session
    (SEC-13: a forged session id can't write into someone else's history).
    `meta` (LLM-12): turn id, route — never part of the model's history."""
    _query(
        "INSERT INTO chat_history (user_id, session_id, tab, role, content, meta) "
        "SELECT %s, %s, %s, %s, %s, %s "
        "WHERE EXISTS (SELECT 1 FROM chat_sessions WHERE id=%s AND user_id=%s)",
        (
            user_id,
            session_id,
            tab,
            role,
            content,
            None if meta is None else json.dumps(meta),
            session_id,
            user_id,
        ),
        fetch=None,
        commit=True,
    )


def chat_load(user_id: int, session_id: int) -> list[dict]:
    """Loads a session's messages — only if it belongs to user_id (tenant isolation)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM chat_sessions WHERE id=%s AND user_id=%s", (session_id, user_id))
        if not cur.fetchone():
            return []
        cur.execute(
            "SELECT role, content, meta FROM chat_history WHERE session_id=%s ORDER BY ts ASC, id ASC",
            (session_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        release_conn(conn)


# ── Report helpers ────────────────────────────────────────────────────────────


def report_save(user_id: int, symbol: str, report_type: str, title: str, content: str) -> int:
    row = _query(
        "INSERT INTO reports (user_id, symbol, report_type, title, content) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (user_id, symbol, report_type, title, content),
        fetch="one",
        commit=True,
    )
    return row["id"]


def report_list(user_id: int, limit: int = 20) -> list[dict]:
    return _query(
        "SELECT id, symbol, report_type, title, created_at FROM reports "
        "WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (user_id, limit),
        fetch="all",
    )


# ── PDF cache helpers ─────────────────────────────────────────────────────────


# "No period" is stored as '' (migration 0003): a NULL period made the unique
# key never match, so every refetch inserted a duplicate row.


def pdf_cache_get(symbol: str, doc_type: str, period: str | None = None) -> str | None:
    """The cached text for exactly this period, else the period-less entry."""
    row = _query(
        "SELECT text_content FROM pdf_cache WHERE symbol=%s AND doc_type=%s AND period IN (%s, '') "
        "ORDER BY (period = %s) DESC, fetched_at DESC LIMIT 1",
        (symbol.upper(), doc_type, period or "", period or ""),
        fetch="one",
    )
    return row["text_content"] if row else None


def pdf_cache_set(symbol: str, doc_type: str, text: str, period: str | None = None, url: str | None = None):
    _query(
        "INSERT INTO pdf_cache (symbol, doc_type, period, url, text_content) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (symbol, doc_type, period) DO UPDATE SET "
        "url=EXCLUDED.url, text_content=EXCLUDED.text_content, fetched_at=now()",
        (symbol.upper(), doc_type, period or "", url, text),
        fetch=None,
        commit=True,
    )


# ── Saved screener helpers ────────────────────────────────────────────────────


def screener_save(user_id: int, name: str, filters: dict):
    _query(
        "INSERT INTO saved_screeners (user_id, name, filters_json) VALUES (%s,%s,%s)",
        (user_id, name, json.dumps(filters)),
        fetch=None,
        commit=True,
    )


def screener_list(user_id: int) -> list[dict]:
    rows = _query(
        "SELECT id, name, filters_json, created_at FROM saved_screeners WHERE user_id=%s",
        (user_id,),
        fetch="all",
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "filters": json.loads(r["filters_json"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ── Alert helpers ─────────────────────────────────────────────────────────────


class DuplicateAlert(ValueError):
    """The same alert is already active (uq_alerts_active_spec, VAL-04)."""

    def __init__(self, existing_id: int | None):
        self.existing_id = existing_id
        super().__init__(f"duplicate of active alert #{existing_id}")


class AlertLimitReached(ValueError):
    """The user already has MAX_ACTIVE_ALERTS active alerts (VAL-04)."""


MAX_ACTIVE_ALERTS = 50


def _existing_alert_id(user_id: int, symbol: str, alert_type: str, threshold, direction) -> int | None:
    row = _query(
        "SELECT id FROM alerts WHERE user_id=%s AND symbol=%s AND alert_type=%s AND active "
        "AND COALESCE(threshold, -1) = COALESCE(%s, -1) AND COALESCE(direction, '') = COALESCE(%s, '')",
        (user_id, symbol.upper(), alert_type, threshold, direction),
        fetch="one",
    )
    return row["id"] if row else None


def alert_add(
    user_id: int,
    symbol: str,
    alert_type: str,
    condition: str,
    threshold: float | None,
    recurring: bool = False,
    cooldown_hours: int = 24,
    direction: str | None = None,
) -> int:
    """Raises DuplicateAlert if the same alert is already active, and
    AlertLimitReached once the user has MAX_ACTIVE_ALERTS active alerts.
    The cap is checked in the INSERT itself, so a burst can't overshoot it
    by more than its own concurrency (and per-user turns run one at a time)."""
    try:
        row = _query(
            "INSERT INTO alerts (user_id, symbol, alert_type, condition, threshold, recurring, cooldown_hours, "
            "direction) "
            "SELECT %s,%s,%s,%s,%s,%s,%s,%s "
            "WHERE (SELECT count(*) FROM alerts WHERE user_id=%s AND active) < %s "
            "RETURNING id",
            (
                user_id,
                symbol.upper(),
                alert_type,
                condition,
                threshold,
                recurring,
                cooldown_hours,
                direction,
                user_id,
                MAX_ACTIVE_ALERTS,
            ),
            fetch="one",
            commit=True,
        )
    except pg_errors.UniqueViolation as e:
        raise DuplicateAlert(_existing_alert_id(user_id, symbol, alert_type, threshold, direction)) from e
    if row is None:
        raise AlertLimitReached(f"you already have {MAX_ACTIVE_ALERTS} active alerts")
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
        (alert_id, user_id),
        fetch=None,
        commit=True,
    )


def alert_resume(user_id: int, alert_id: int):
    """Raises DuplicateAlert if an identical alert is already active."""
    try:
        _query(
            "UPDATE alerts SET active=TRUE WHERE id=%s AND user_id=%s",
            (alert_id, user_id),
            fetch=None,
            commit=True,
        )
    except pg_errors.UniqueViolation as e:
        raise DuplicateAlert(None) from e


# ── In-app notification inbox (DATA-08) ───────────────────────────────────────
# The fallback delivery channel for alerts: written by scripts/check_alerts.py
# when Telegram isn't linked or keeps failing, read in the Settings tab.


def notification_add(user_id: int, message: str, alert_id: int | None = None) -> int:
    row = _query(
        "INSERT INTO notifications (user_id, alert_id, message) VALUES (%s, %s, %s) RETURNING id",
        (user_id, alert_id, message),
        fetch="one",
        commit=True,
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
        "SELECT count(*) AS n FROM notifications WHERE user_id=%s AND read_at IS NULL",
        (user_id,),
        fetch="one",
    )
    return int(row["n"]) if row else 0


# DATA-08: reading an inbox entry is what consumes an alert that was queued
# there (scripts/check_alerts.py leaves it active as 'pending_delivery').
# One statement, so a notification is never marked read without its alert
# being consumed. {only_ids} is one of two constant fragments.
_MARK_READ_SQL = """
    WITH newly_read AS (
        UPDATE notifications SET read_at=now()
        WHERE user_id=%(uid)s AND read_at IS NULL {only_ids}
        RETURNING alert_id
    )
    UPDATE alerts SET
        active = CASE WHEN recurring THEN active ELSE FALSE END,
        triggered_at = now(), delivery_status = 'in_app',
        delivery_attempts = 0, last_delivery_error = NULL
    WHERE user_id=%(uid)s AND delivery_status='pending_delivery'
      AND id IN (SELECT alert_id FROM newly_read WHERE alert_id IS NOT NULL)
"""


def notifications_mark_read(user_id: int, ids: list[int] | None = None) -> None:
    """Marks the given notifications (or all unread ones) read — only the
    caller's own rows, whatever ids are passed — and consumes any alert that
    was waiting on them (see _MARK_READ_SQL)."""
    if ids is None:
        sql, params = _MARK_READ_SQL.format(only_ids=""), {"uid": user_id}
    elif ids:
        sql = _MARK_READ_SQL.format(only_ids="AND id = ANY(%(ids)s)")
        params = {"uid": user_id, "ids": list(ids)}
    else:
        return
    _query(sql, params, fetch=None, commit=True)


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
        (user_id, json.dumps(current), json.dumps(current)),
        fetch=None,
        commit=True,
    )
    return current


# ── Evidence cache helpers (research graph, see tools/data/evidence_cache.py) ─


def evidence_cache_get(tool: str, args_hash: str) -> dict | None:
    return _query(
        "SELECT payload, confidence, fetched_at FROM evidence_cache WHERE tool=%s AND args_hash=%s",
        (tool, args_hash),
        fetch="one",
    )


def evidence_cache_put(
    tool: str, args_hash: str, entity: str, evidence_type: str, payload: dict, confidence: float = 1.0
) -> None:
    _query(
        "INSERT INTO evidence_cache (tool, args_hash, entity, evidence_type, payload, confidence) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (tool, args_hash) DO UPDATE "
        "SET payload=EXCLUDED.payload, confidence=EXCLUDED.confidence, fetched_at=now()",
        (tool, args_hash, entity, evidence_type, json.dumps(payload, default=str), confidence),
        fetch=None,
        commit=True,
    )


def profile_clear(user_id: int):
    _query("DELETE FROM financial_profiles WHERE user_id=%s", (user_id,), fetch=None, commit=True)


# ── LLM config helpers (bring-your-own-key, see utils/crypto.py) ─────────────


def llm_config_get(user_id: int) -> dict | None:
    return _query(
        "SELECT provider, base_url, model, api_key_encrypted FROM user_llm_config WHERE user_id=%s",
        (user_id,),
        fetch="one",
    )


def llm_config_set(
    user_id: int, provider: str, model: str, api_key_encrypted: str, base_url: str | None = None
):
    _query(
        "INSERT INTO user_llm_config (user_id, provider, base_url, model, api_key_encrypted, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,now()) "
        "ON CONFLICT (user_id) DO UPDATE SET provider=%s, base_url=%s, model=%s, api_key_encrypted=%s, updated_at=now()",
        (user_id, provider, base_url, model, api_key_encrypted, provider, base_url, model, api_key_encrypted),
        fetch=None,
        commit=True,
    )


def llm_config_clear(user_id: int):
    _query("DELETE FROM user_llm_config WHERE user_id=%s", (user_id,), fetch=None, commit=True)


def llm_config_ciphertexts() -> list[dict]:
    """Every stored BYO key ciphertext, for operator key rotation (SEC-11,
    scripts/rotate_secret_key.py). Cross-tenant by design; never call from
    a request handler."""
    return _query("SELECT user_id, api_key_encrypted FROM user_llm_config ORDER BY user_id") or []


def llm_config_rewrap(user_id: int, old_ciphertext: str, new_ciphertext: str) -> bool:
    """Replace one user's ciphertext only if it is still `old_ciphertext`
    (compare-and-set, so a key the user saved mid-rotation is not clobbered)."""
    row = _query(
        "UPDATE user_llm_config SET api_key_encrypted=%s WHERE user_id=%s AND api_key_encrypted=%s RETURNING user_id",
        (new_ciphertext, user_id, old_ciphertext),
        fetch="one",
        commit=True,
    )
    return row is not None


# ── Free-trial quota helpers (agents/graph_orchestrator.py) ──────────────────


def trial_tokens_used(user_id: int, today: "date") -> int:
    """LLM tokens this trial user has spent today (LLM-11)."""
    row = _query(
        "SELECT CASE WHEN trial_tokens_day = %s THEN trial_tokens_used ELSE 0 END AS used FROM users WHERE id=%s",
        (today, user_id),
        fetch="one",
    )
    return int(row["used"]) if row else 0


def trial_tokens_add(user_id: int, tokens: int, today: "date") -> int:
    """Add a turn's tokens to today's count, in one statement (resets on a new
    day). Returns the new total."""
    row = _query(
        "UPDATE users SET trial_tokens_used = CASE WHEN trial_tokens_day = %(today)s "
        "  THEN trial_tokens_used + %(n)s ELSE %(n)s END, trial_tokens_day = %(today)s "
        "WHERE id = %(uid)s RETURNING trial_tokens_used",
        {"today": today, "n": int(tokens), "uid": user_id},
        fetch="one",
        commit=True,
    )
    return int(row["trial_tokens_used"]) if row else 0


def trial_quota_check_and_increment(user_id: int, daily_limit: int, today: "date") -> bool:
    """Atomically checks-and-increments a user's free-trial message count for
    `today` (an IST calendar date, passed in rather than computed here so
    every caller uses the same clock read). Returns True if this message is
    allowed. Resets the counter the first time a new day is seen — no
    separate cron/cleanup job needed, the reset just happens lazily on next
    use, same pattern as the alert cooldown check in scripts/check_alerts.py.

    One conditional UPDATE, so the check and the increment can't interleave:
    the old read-then-write let concurrent messages (two tabs, two replicas)
    all see "under the limit" and all get through (SEC-12)."""
    if daily_limit <= 0:
        return False
    row = _query(
        "UPDATE users SET "
        "  free_msgs_used_today = CASE WHEN free_msgs_reset_at IS DISTINCT FROM %(today)s "
        "                              THEN 1 ELSE free_msgs_used_today + 1 END, "
        "  free_msgs_reset_at = %(today)s "
        "WHERE id = %(uid)s "
        "  AND (free_msgs_reset_at IS DISTINCT FROM %(today)s OR free_msgs_used_today < %(limit)s) "
        "RETURNING free_msgs_used_today",
        {"today": today, "uid": user_id, "limit": daily_limit},
        fetch="one",
        commit=True,
    )
    return row is not None


# ── Telegram linking helpers (utils/telegram_link.py) ─────────────────────────


def telegram_link_code_create(user_id: int, code: str, expires_at) -> None:
    _query(
        "INSERT INTO telegram_link_codes (code, user_id, expires_at) VALUES (%s,%s,%s)",
        (code, user_id, expires_at),
        fetch=None,
        commit=True,
    )


def telegram_link_code_resolve(code: str):
    """Returns the owning user_id if `code` exists and hasn't expired, else
    None. Deletes the code either way (single-use) so a stale/guessed code
    can't be retried. DELETE ... RETURNING claims it atomically — the old
    SELECT-then-DELETE let two concurrent /start messages both link."""
    row = _query(
        "DELETE FROM telegram_link_codes WHERE code=%s RETURNING user_id, expires_at",
        (code,),
        fetch="one",
        commit=True,
    )
    if row is None:
        return None
    from datetime import datetime

    if row["expires_at"] < datetime.now(UTC):
        return None
    return row["user_id"]


def telegram_chat_id_set(user_id: int, chat_id: str) -> None:
    _query("UPDATE users SET telegram_chat_id=%s WHERE id=%s", (chat_id, user_id), fetch=None, commit=True)


def telegram_chat_id_get(user_id: int) -> str | None:
    row = _query("SELECT telegram_chat_id FROM users WHERE id=%s", (user_id,), fetch="one")
    return row["telegram_chat_id"] if row else None


# ── Terms-of-service consent (utils/auth.py) ──────────────────────────────────


def accept_terms(user_id: int, version: str | None = None) -> None:
    """Record that the user accepted the Terms (COMP-02: which version, and when)."""
    if version is None:
        from utils.terms import current_version

        version = current_version()
    _query(
        "UPDATE users SET accepted_terms_at=now(), accepted_terms_version=%s WHERE id=%s",
        (version, user_id),
        fetch=None,
        commit=True,
    )


def terms_version_get(version: str) -> dict | None:
    return _query(
        "SELECT version, effective_at, material, summary FROM terms_versions WHERE version=%s",
        (version,),
        fetch="one",
    )


# ── Answer feedback (LLM-12) ─────────────────────────────────────────────────


def feedback_record(
    user_id: int,
    turn_id: str,
    rating: int,
    session_id: int | None = None,
    reason: str | None = None,
    free_text: str | None = None,
) -> None:
    """Rate an answer (1 / -1); rating again updates the same row."""
    _query(
        "INSERT INTO answer_feedback (user_id, turn_id, session_id, rating, reason, free_text) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (user_id, turn_id) DO UPDATE SET rating=EXCLUDED.rating, "
        "reason=COALESCE(EXCLUDED.reason, answer_feedback.reason), "
        "free_text=COALESCE(EXCLUDED.free_text, answer_feedback.free_text), updated_at=now()",
        (user_id, turn_id, session_id, rating, reason, (free_text or "")[:2000] or None),
        fetch=None,
        commit=True,
    )


def feedback_for(user_id: int) -> list[dict]:
    """A user's own ratings (data export / support)."""
    return (
        _query(
            "SELECT turn_id, rating, reason, free_text, created_at FROM answer_feedback WHERE user_id=%s "
            "ORDER BY created_at DESC",
            (user_id,),
        )
        or []
    )


def feedback_negative_since(since, include_triaged: bool = False) -> list[dict]:
    """Operator triage (cross-tenant by design): thumbs-down since `since`,
    with the question and answer they were about."""
    return (
        _query(
            "SELECT f.id, f.turn_id, f.reason, f.free_text, f.created_at, t.intent, t.status, "
            "  (SELECT content FROM chat_history h WHERE h.meta->>'turn_id' = f.turn_id AND h.role='assistant' "
            "   LIMIT 1) AS answer, "
            "  (SELECT content FROM chat_history q WHERE q.session_id = f.session_id AND q.role='user' "
            "   AND q.id < (SELECT id FROM chat_history h2 WHERE h2.meta->>'turn_id' = f.turn_id LIMIT 1) "
            "   ORDER BY q.id DESC LIMIT 1) AS question "
            "FROM answer_feedback f LEFT JOIN turns t ON t.turn_id::text = f.turn_id "
            "WHERE f.rating = -1 AND f.created_at >= %s AND (%s OR f.triaged_at IS NULL) "
            "ORDER BY f.created_at DESC",
            (since, include_triaged),
        )
        or []
    )


def feedback_mark_triaged(feedback_ids: list[int]) -> int:
    rows = _query(
        "UPDATE answer_feedback SET triaged_at=now() WHERE id = ANY(%s) RETURNING id",
        (feedback_ids,),
        fetch="all",
        commit=True,
    )
    return len(rows or [])


def feedback_rate(since) -> dict:
    """Rated answers and the negative share since `since` (OBS-03)."""
    row = _query(
        "SELECT count(*) AS rated, count(*) FILTER (WHERE rating = -1) AS negative FROM answer_feedback "
        "WHERE created_at >= %s",
        (since,),
        fetch="one",
    ) or {"rated": 0, "negative": 0}
    return {"rated": row["rated"], "negative": row["negative"]}


# ── Audit log (COMP-04: append-only, enforced by a trigger) ───────────────────


def audit_log_write(
    action: str,
    *,
    actor_user_id: int | None = None,
    target: str | None = None,
    details: dict | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    _query(
        "INSERT INTO audit_log (actor_user_id, action, target, details, ip, user_agent) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (actor_user_id, action, target, None if details is None else json.dumps(details), ip, user_agent),
        fetch=None,
        commit=True,
    )


def audit_log_for(actor_user_id: int, limit: int = 1000) -> list[dict]:
    """One account's own audit trail, newest first (data export, support)."""
    return _query(
        "SELECT at, action, target, details, ip FROM audit_log WHERE actor_user_id=%s ORDER BY at DESC LIMIT %s",
        (actor_user_id, limit),
    )


# ── Data export (COMP-03) ─────────────────────────────────────────────────────


def user_scoped_tables() -> list[str]:
    """Every public table with a user_id column, read from the live schema."""
    rows = _query(
        "SELECT DISTINCT table_name FROM information_schema.columns "
        "WHERE table_schema='public' AND column_name='user_id' ORDER BY table_name"
    )
    return [r["table_name"] for r in rows or []]


def user_rows(table: str, user_id: int) -> list[dict]:
    """All of one user's rows in `table` (a name from user_scoped_tables())."""
    if table not in user_scoped_tables():
        raise ValueError(f"not a user-scoped table: {table!r}")
    from psycopg import sql

    query = sql.SQL("SELECT * FROM {} WHERE user_id = %s").format(sql.Identifier(table))
    return _query(query.as_string(), (user_id,)) or []


def user_account_row(user_id: int) -> dict | None:
    return _query("SELECT * FROM users WHERE id=%s", (user_id,), fetch="one")


# ── Accounts: verification, reset, deletion (SEC-06) ──────────────────────────


def user_by_email(email: str) -> dict | None:
    return _query(
        "SELECT id, email, email_verified_at FROM users WHERE email=%s", (email.strip().lower(),), fetch="one"
    )


def user_email(user_id: int) -> str | None:
    row = _query("SELECT email FROM users WHERE id=%s", (user_id,), fetch="one")
    return row["email"] if row else None


def user_password_hash(user_id: int) -> str | None:
    row = _query("SELECT password_hash FROM users WHERE id=%s", (user_id,), fetch="one")
    return row["password_hash"] if row else None


def user_rehash_password(user_id: int, password_hash: str) -> None:
    """Swap in a stronger hash of the same password (SEC-07). Sessions stay."""
    _query(
        "UPDATE users SET password_hash=%s WHERE id=%s",
        (password_hash, user_id),
        fetch=None,
        commit=True,
    )


def user_mark_verified(user_id: int) -> None:
    _query(
        "UPDATE users SET email_verified_at = COALESCE(email_verified_at, now()) WHERE id=%s",
        (user_id,),
        fetch=None,
        commit=True,
    )


def user_set_password(user_id: int, password_hash: str) -> None:
    """New password; every login session of the user is revoked with it."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hash, user_id))
        cur.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        release_conn(conn)


def auth_token_create(user_id: int, purpose: str, token_hash: str, expires_at) -> None:
    """Store a single-use token; any earlier unused token for the same
    purpose is invalidated, so only the newest emailed link works."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE auth_tokens SET used_at=now() WHERE user_id=%s AND purpose=%s AND used_at IS NULL",
            (user_id, purpose),
        )
        cur.execute(
            "INSERT INTO auth_tokens (user_id, purpose, token_hash, expires_at) VALUES (%s,%s,%s,%s)",
            (user_id, purpose, token_hash, expires_at),
        )
        conn.commit()
    finally:
        release_conn(conn)


def auth_token_consume(purpose: str, token_hash: str) -> int | None:
    """Atomically use a token: the user id if it exists, matches the purpose,
    is unused and unexpired — else None. A second use returns None."""
    row = _query(
        "UPDATE auth_tokens SET used_at=now() "
        "WHERE token_hash=%s AND purpose=%s AND used_at IS NULL AND expires_at > now() RETURNING user_id",
        (token_hash, purpose),
        fetch="one",
        commit=True,
    )
    return row["user_id"] if row else None


def user_delete(user_id: int) -> bool:
    """Delete an account and everything it owns, in one transaction. Every
    user-owned table cascades from users (migrations 0002+); rate-limit rows
    keyed by the email are removed too. The audit trail (no FK) remains."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT email FROM users WHERE id=%s FOR UPDATE", (user_id,))
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return False
        cur.execute(
            "DELETE FROM auth_attempts WHERE key = ANY(%s)",
            ([f"login:{row['email']}", f"signup:{row['email']}", f"reset:{row['email']}"],),
        )
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        conn.commit()
        return True
    finally:
        release_conn(conn)


# ── Login sessions (SEC-04: cookie sessions, only token hashes stored) ────────


def auth_session_create(
    user_id: int, token_hash: str, expires_at, ip: str | None = None, user_agent: str | None = None
) -> None:
    _query(
        "INSERT INTO sessions (user_id, token_hash, expires_at, ip, user_agent) VALUES (%s,%s,%s,%s,%s)",
        (user_id, token_hash, expires_at, ip, (user_agent or "")[:300] or None),
        fetch=None,
        commit=True,
    )


def auth_session_lookup(token_hash: str) -> dict | None:
    """The live (unexpired) session for a token hash: user_id, expires_at,
    last_seen_at, verified (email confirmed), consent_ok (accepted the latest
    material Terms version, COMP-02) — or None."""
    return _query(
        "SELECT s.user_id, s.expires_at, s.last_seen_at, (u.email_verified_at IS NOT NULL) AS verified, "
        "  COALESCE(u.accepted_terms_version >= ("
        "    SELECT max(version) FROM terms_versions WHERE material AND effective_at <= now()"
        "  ), TRUE) AND u.accepted_terms_version IS NOT NULL AS consent_ok "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=%s AND s.expires_at > now()",
        (token_hash,),
        fetch="one",
    )


def auth_session_touch(token_hash: str) -> None:
    _query(
        "UPDATE sessions SET last_seen_at=now() WHERE token_hash=%s", (token_hash,), fetch=None, commit=True
    )


def auth_session_delete(token_hash: str) -> None:
    _query("DELETE FROM sessions WHERE token_hash=%s", (token_hash,), fetch=None, commit=True)


def auth_sessions_list(user_id: int) -> list[dict]:
    """The user's live sessions, newest first (no token hashes)."""
    return _query(
        "SELECT id, created_at, last_seen_at, expires_at, ip, user_agent FROM sessions "
        "WHERE user_id=%s AND expires_at > now() ORDER BY last_seen_at DESC",
        (user_id,),
    )


def auth_sessions_revoke_others(user_id: int, keep_token_hash: str | None) -> int:
    """'Log out other devices': delete every session of this user except the
    one making the request. Returns how many were revoked."""
    rows = _query(
        "DELETE FROM sessions WHERE user_id=%s AND token_hash IS DISTINCT FROM %s RETURNING id",
        (user_id, keep_token_hash),
        fetch="all",
        commit=True,
    )
    return len(rows or [])


def auth_sessions_purge_expired() -> int:
    """Daily job: drop expired login sessions. Returns rows deleted."""
    rows = _query("DELETE FROM sessions WHERE expires_at <= now() RETURNING id", fetch="all", commit=True)
    return len(rows or [])


# ── Postgres-backed auth rate limiting (replaces utils/auth.py's in-memory dict) ─


def auth_attempt_record(key: str) -> None:
    _query("INSERT INTO auth_attempts (key) VALUES (%s)", (key,), fetch=None, commit=True)


def auth_attempt_count(key: str, since) -> int:
    row = _query(
        "SELECT COUNT(*) AS n FROM auth_attempts WHERE key=%s AND attempted_at >= %s",
        (key, since),
        fetch="one",
    )
    return row["n"] if row else 0


def auth_attempt_clear(key: str) -> None:
    _query("DELETE FROM auth_attempts WHERE key=%s", (key,), fetch=None, commit=True)


def rate_limit_take(key: str, limit: int, window_seconds: float) -> bool:
    """Atomically record one event for `key` if fewer than `limit` happened in
    the last `window_seconds` (SEC-12). True if allowed. One statement, so
    every replica shares the same budget. A transaction-scoped advisory lock
    on the key serialises concurrent takes, so a burst can't overshoot."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
        cur.execute(
            "INSERT INTO auth_attempts (key) SELECT %s "
            "WHERE (SELECT count(*) FROM auth_attempts WHERE key=%s "
            "       AND attempted_at > now() - make_interval(secs => %s)) < %s "
            "RETURNING id",
            (key, key, window_seconds, limit),
        )
        allowed = cur.fetchone() is not None
        conn.commit()
        return allowed
    finally:
        release_conn(conn)


def auth_attempt_last(key: str):
    """When `key` last made an attempt (None if never / purged)."""
    row = _query("SELECT max(attempted_at) AS last FROM auth_attempts WHERE key=%s", (key,), fetch="one")
    return row["last"] if row else None


def auth_attempts_purge(older_than) -> int:
    """Housekeeping (SEC-05): drop attempt rows older than `older_than`, so the
    table stays bounded. Returns rows deleted."""
    rows = _query(
        "DELETE FROM auth_attempts WHERE attempted_at < %s RETURNING id",
        (older_than,),
        fetch="all",
        commit=True,
    )
    return len(rows or [])


def auth_tokens_purge_expired() -> int:
    """Housekeeping: drop verification/reset tokens a day after they expired."""
    rows = _query(
        "DELETE FROM auth_tokens WHERE expires_at < now() - interval '1 day' RETURNING id",
        fetch="all",
        commit=True,
    )
    return len(rows or [])


# ── Report content helper (regenerate-on-demand downloads, see app.py) ───────


def report_get(user_id: int, report_id: int) -> dict | None:
    """Fetches one report's full content — only if owned by user_id (tenant
    isolation, mirrors session_delete/alert_remove's ownership-check pattern)."""
    return _query(
        "SELECT id, symbol, report_type, title, content, created_at FROM reports WHERE id=%s AND user_id=%s",
        (report_id, user_id),
        fetch="one",
    )


# ── Per-turn records (OBS-02, written by utils/tracing.py) ───────────────────


def turn_record(
    *,
    turn_id: str,
    user_id: int | None,
    session_id: str | None,
    intent: str | None,
    complexity: str | None,
    status: str,
    error_id: str,
    latency_ms: int,
    ttft_ms: int | None,
    llm_calls: int,
    tokens_in: int,
    tokens_out: int,
    tokens_estimated: bool,
    providers: list[str],
    prompt_versions: dict,
    tool_calls: int,
    cache_hits: int,
    node_ms: dict,
    data_gaps: int,
    grounding: dict | None,
    parse_failures: dict | None = None,
    cost_inr: float = 0.0,
    llm_by_node: dict | None = None,
    models: list[str] | None = None,
) -> None:
    _query(
        "INSERT INTO turns (turn_id, user_id, session_id, intent, complexity, status, error_id, latency_ms, "
        "ttft_ms, llm_calls, tokens_in, tokens_out, tokens_estimated, providers, prompt_versions, tool_calls, "
        "cache_hits, node_ms, data_gaps, grounding, parse_failures, cost_inr, llm_by_node, models) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (turn_id) DO NOTHING",
        (
            turn_id,
            user_id,
            None if session_id is None else str(session_id),
            intent,
            complexity,
            status,
            error_id,
            latency_ms,
            ttft_ms,
            llm_calls,
            tokens_in,
            tokens_out,
            tokens_estimated,
            providers,
            json.dumps(prompt_versions),
            tool_calls,
            cache_hits,
            json.dumps(node_ms),
            data_gaps,
            None if grounding is None else json.dumps(grounding, default=str),
            None if parse_failures is None else json.dumps(parse_failures),
            cost_inr,
            None if llm_by_node is None else json.dumps(llm_by_node),
            models or [],
        ),
        fetch=None,
        commit=True,
    )


def turn_set_trace_url(turn_id: str, url: str) -> None:
    _query("UPDATE turns SET trace_url=%s WHERE turn_id=%s", (url, turn_id), fetch=None, commit=True)


def turns_by_error_id(error_id: str) -> list[dict]:
    """Support lookup (scripts/find_turn.py): every turn whose error ID
    matches — operator-only, deliberately not user-scoped."""
    return _query(
        "SELECT * FROM turns WHERE error_id=%s ORDER BY created_at DESC",
        ((error_id or "").strip().lower(),),
    )


# ── Mutual fund metrics cache (DATA-04, tools/data/mf_data.py) ───────────────

_MF_METRIC_COLS = (
    "scheme_name",
    "category",
    "nav",
    "nav_date",
    "history_start",
    "cagr_1y",
    "cagr_3y",
    "cagr_5y",
    "volatility_1y",
    "max_drawdown_3y",
    "consistency_1y",
)


def mf_metrics_get(codes: list[str], computed_since: date) -> dict[str, dict]:
    """Cached metrics for `codes` computed on or after `computed_since`."""
    if not codes:
        return {}
    rows = _query(
        "SELECT * FROM mf_metrics WHERE scheme_code = ANY(%s) AND computed_at::date >= %s",
        (list(codes), computed_since),
    )
    return {r["scheme_code"]: {k: v for k, v in r.items() if k != "scheme_code"} for r in rows}


# Built once from the constant column list above — no caller input reaches it.
_MF_UPSERT_SQL = (
    f"INSERT INTO mf_metrics (scheme_code, {', '.join(_MF_METRIC_COLS)}) "  # noqa: S608  # nosec B608
    f"VALUES ({', '.join(['%s'] * (len(_MF_METRIC_COLS) + 1))}) "
    f"ON CONFLICT (scheme_code) DO UPDATE SET {', '.join(f'{c}=EXCLUDED.{c}' for c in _MF_METRIC_COLS)}, "
    "computed_at=now()"
)


def mf_metrics_put(rows: list[dict]) -> None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        for row in rows:
            cur.execute(_MF_UPSERT_SQL, (row["scheme_code"], *[row.get(c) for c in _MF_METRIC_COLS]))
        conn.commit()
    finally:
        release_conn(conn)


# ── Fundamentals snapshot (DATA-05; scripts/refresh_fundamentals.py) ─────────

FUNDAMENTALS_COLUMNS = (
    "symbol",
    "isin",
    "name",
    "sector",
    "industry",
    "lender",
    "price",
    "mcap_cr",
    "pe",
    "pb",
    "roe",
    "roce",
    "de",
    "div_yield",
    "rev_growth",
    "from_52w_high",
    "from_52w_low",
)
# Screen fields that may appear in a WHERE clause (VAL-06 names -> columns).
SCREEN_COLUMNS = {
    "pe": "pe",
    "pb": "pb",
    "roe": "roe",
    "roce": "roce",
    "de": "de",
    "mcap": "mcap_cr",
    "div_yield": "div_yield",
    "rev_growth": "rev_growth",
    "price": "price",
    "from_52w_high": "from_52w_high",
    "from_52w_low": "from_52w_low",
}
_SCREEN_OPS = {"<": "<", "<=": "<=", ">": ">", ">=": ">="}


def fundamentals_upsert(rows: list[dict], *, keep_existing: bool = False) -> int:
    """Insert or refresh snapshot rows (as_of = now). Returns rows sent.

    keep_existing=True only inserts symbols with no row yet: a degraded quote
    (price only, from the bhavcopy fallback) must not blank out a complete
    row from an earlier run."""
    if not rows:
        return 0
    cols = ", ".join(FUNDAMENTALS_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in FUNDAMENTALS_COLUMNS)
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in FUNDAMENTALS_COLUMNS if c != "symbol")
    on_conflict = "DO NOTHING" if keep_existing else f"DO UPDATE SET {updates}, as_of=now()"  # nosec B608 - fixed column list
    sql = (
        f"INSERT INTO fundamentals_snapshot ({cols}) VALUES ({placeholders}) "  # noqa: S608 (fixed column list)  # nosec B608
        f"ON CONFLICT (symbol) {on_conflict}"
    )
    payload = [{c: r.get(c) for c in FUNDAMENTALS_COLUMNS} | {"lender": bool(r.get("lender"))} for r in rows]
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.executemany(sql, payload)
        conn.commit()
    finally:
        release_conn(conn)
    return len(payload)


def fundamentals_screen(
    filters: list[tuple[str, str, float, float | None]],
    sectors: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """Rows passing every (field, op, value, high) filter, largest first.
    Fields and operators are whitelisted (SCREEN_COLUMNS / _SCREEN_OPS);
    values are always bound parameters. A NULL metric fails its filter."""
    where: list[str] = []
    params: list[Any] = []
    for field, op, value, high in filters:
        col = SCREEN_COLUMNS.get(field)
        if col is None:
            raise ValueError(f"not a screen field: {field}")
        if op == "between":
            where.append(f"{col} BETWEEN %s AND %s")
            params += [value, high if high is not None else value]
        elif op == "=":
            where.append(f"abs({col} - %s) <= greatest(abs(%s) * 0.01, 0.01)")
            params += [value, value]
        elif op in _SCREEN_OPS:
            where.append(f"{col} {_SCREEN_OPS[op]} %s")
            params.append(value)
        else:
            raise ValueError(f"not a screen operator: {op}")
    if sectors:
        where.append("sector = ANY(%s)")
        params.append(list(sectors))
    sql = (
        f"SELECT {', '.join(FUNDAMENTALS_COLUMNS)}, as_of FROM fundamentals_snapshot "  # noqa: S608 (whitelisted)  # nosec B608
        f"{'WHERE ' + ' AND '.join(where) if where else ''} "
        "ORDER BY mcap_cr DESC NULLS LAST LIMIT %s"
    )
    params.append(limit)
    return _query(sql, tuple(params)) or []


def fundamentals_status() -> dict:
    """{rows, oldest, newest} for staleness checks (screener note, OBS-04)."""
    row = _query(
        "SELECT count(*) AS rows, min(as_of) AS oldest, max(as_of) AS newest FROM fundamentals_snapshot",
        fetch="one",
    )
    return row or {"rows": 0, "oldest": None, "newest": None}


def fundamentals_fresh_symbols(since) -> set[str]:
    """Symbols refreshed at or after `since` with real fundamentals — a resumed
    run skips them. A price-only row (degraded quote) isn't fresh: retry it."""
    rows = _query(
        "SELECT symbol FROM fundamentals_snapshot WHERE as_of >= %s "
        "AND (mcap_cr IS NOT NULL OR pe IS NOT NULL OR sector IS NOT NULL)",
        (since,),
    )
    return {r["symbol"] for r in rows or []}


def fundamentals_peers(symbol: str, limit: int = 5) -> dict:
    """{self, peers, basis, lender}: up to `limit` companies in the same
    industry (else sector) closest in market cap, from the snapshot (UX-04)."""
    cols = ", ".join(FUNDAMENTALS_COLUMNS)
    me = _query(f"SELECT {cols} FROM fundamentals_snapshot WHERE symbol=%s", (symbol,), fetch="one")  # noqa: S608  # nosec B608
    if not me:
        return {"self": None, "peers": [], "basis": None, "lender": False}
    for basis in ("industry", "sector"):
        if not me.get(basis):
            continue
        peers = _query(
            f"SELECT {cols} FROM fundamentals_snapshot "  # noqa: S608 (fixed columns; basis is one of two names)  # nosec B608
            f"WHERE {basis}=%s AND symbol<>%s AND mcap_cr IS NOT NULL "
            "ORDER BY abs(ln(greatest(mcap_cr, 1)) - ln(greatest(coalesce(%s, mcap_cr), 1))) LIMIT %s",
            (me[basis], symbol, me.get("mcap_cr"), limit),
        )
        if len(peers or []) >= 2 or basis == "sector":
            return {
                "self": me,
                "peers": peers or [],
                "basis": f"{basis}: {me[basis]}",
                "lender": me["lender"],
            }
    return {"self": me, "peers": [], "basis": None, "lender": me["lender"]}


def telegram_link_codes_purge_expired() -> int:
    """Housekeeping: unused Telegram link codes past their expiry."""
    rows = _query("DELETE FROM telegram_link_codes WHERE expires_at < now() RETURNING code", commit=True)
    return len(rows or [])


# ── Background job heartbeats (REL-05; utils/jobs.py) ────────────────────────


def job_heartbeat_start(job: str, owner: str) -> None:
    _query(
        "INSERT INTO job_heartbeats (job, owner, last_started_at, last_status) VALUES (%s, %s, now(), 'running') "
        "ON CONFLICT (job) DO UPDATE SET owner=EXCLUDED.owner, last_started_at=now(), last_status='running'",
        (job, owner),
        fetch=None,
        commit=True,
    )


def job_heartbeat_finish(job: str, ok: bool, duration_ms: int, error: str | None = None) -> None:
    _query(
        "UPDATE job_heartbeats SET last_finished_at=now(), last_status=%s, last_error=%s, "
        "last_duration_ms=%s, runs=runs+1, failures=failures + %s, "
        "consecutive_failures=CASE WHEN %s THEN 0 ELSE consecutive_failures + 1 END, "
        "last_ok_at=CASE WHEN %s THEN now() ELSE last_ok_at END WHERE job=%s",
        ("ok" if ok else "error", error, duration_ms, 0 if ok else 1, ok, ok, job),
        fetch=None,
        commit=True,
    )


def job_heartbeats() -> list[dict]:
    return _query("SELECT * FROM job_heartbeats ORDER BY job") or []


# ── Downloads (OPS-02; web/download_routes.py) ──────────────────────────────

DOWNLOAD_TTL_HOURS = 24
MAX_DOWNLOAD_BYTES = 5_000_000


def download_put(user_id: int, filename: str, content_type: str, content: bytes) -> str:
    """Store a generated file for its owner; returns the unguessable token."""
    import secrets

    if len(content) > MAX_DOWNLOAD_BYTES:
        raise ValueError("download too large")
    token = secrets.token_urlsafe(24)
    _query(
        "INSERT INTO downloads (token, user_id, filename, content_type, content, expires_at) "
        "VALUES (%s,%s,%s,%s,%s, now() + make_interval(hours => %s))",
        (token, user_id, filename, content_type, content, DOWNLOAD_TTL_HOURS),
        fetch=None,
        commit=True,
    )
    return token


def download_get(user_id: int, token: str) -> dict | None:
    """The file, only for its owner and only until it expires."""
    return _query(
        "SELECT filename, content_type, content FROM downloads WHERE token=%s AND user_id=%s AND expires_at > now()",
        (token, user_id),
        fetch="one",
    )


def downloads_purge_expired() -> int:
    rows = _query("DELETE FROM downloads WHERE expires_at < now() RETURNING token", commit=True)
    return len(rows or [])


# ── SLO (OBS-03; scripts/slo_report.py, ops watchdog) ───────────────────────

FAILED_TURN_STATUSES = ("error", "llm_unavailable")


def slo_summary(since) -> dict:
    """Turns, good turns and the equity p95 time to first output since `since`."""
    row = _query(
        "SELECT count(*) AS turns, count(*) FILTER (WHERE NOT (status = ANY(%s))) AS good, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY ttft_ms) "
        "FILTER (WHERE intent = 'equity' AND ttft_ms IS NOT NULL) AS equity_p95_ttft_ms "
        "FROM turns WHERE created_at >= %s",
        (list(FAILED_TURN_STATUSES), since),
        fetch="one",
    )
    return row or {"turns": 0, "good": 0, "equity_p95_ttft_ms": None}


# ── Ops watchdog (OBS-04; utils/watchdog.py) ─────────────────────────────────


def turn_window_stats(since) -> dict:
    """Volume, failures, LLM-exhausted turns and p95s for turns since `since`."""
    row = _query(
        "SELECT count(*) AS turns, count(*) FILTER (WHERE status = ANY(%s)) AS failed, "
        "count(*) FILTER (WHERE status = 'llm_unavailable') AS llm_unavailable, "
        "count(*) FILTER (WHERE intent = 'equity' AND ttft_ms IS NOT NULL) AS equity_turns, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY ttft_ms) "
        "FILTER (WHERE intent = 'equity' AND ttft_ms IS NOT NULL) AS equity_p95_ttft_ms "
        "FROM turns WHERE created_at >= %s",
        (list(FAILED_TURN_STATUSES), since),
        fetch="one",
    )
    return row or {}


def db_usage() -> dict:
    """Connections in use vs the server's limit, and this database's size."""
    row = _query(
        "SELECT (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()) AS connections, "
        "current_setting('max_connections')::int AS max_connections, "
        "pg_database_size(current_database()) AS size_bytes",
        fetch="one",
    )
    return row or {}


def ops_alert_fire(key: str, title: str, detail: str, renotify_after_s: int) -> bool:
    """Record that alert `key` is firing; True when the operator should be
    told now (new, re-opened, or a reminder is due)."""
    row = _query(
        "INSERT INTO ops_alerts (key, title, detail) VALUES (%s, %s, %s) "
        "ON CONFLICT (key) DO UPDATE SET title=EXCLUDED.title, detail=EXCLUDED.detail, "
        "  first_fired_at = CASE WHEN ops_alerts.resolved_at IS NOT NULL THEN now() ELSE ops_alerts.first_fired_at END, "
        "  notifications = CASE WHEN ops_alerts.resolved_at IS NOT NULL "
        "      OR ops_alerts.last_notified_at < now() - make_interval(secs => %s) "
        "    THEN ops_alerts.notifications + 1 ELSE ops_alerts.notifications END, "
        "  last_notified_at = CASE WHEN ops_alerts.resolved_at IS NOT NULL "
        "      OR ops_alerts.last_notified_at < now() - make_interval(secs => %s) "
        "    THEN now() ELSE ops_alerts.last_notified_at END, "
        "  resolved_at = NULL "
        "RETURNING (xmax = 0) AS inserted, last_notified_at = now() AS notify",
        (key, title, detail, renotify_after_s, renotify_after_s),
        fetch="one",
        commit=True,
    )
    return bool(row and (row["inserted"] or row["notify"]))


def ops_alert_resolve(key: str) -> dict | None:
    """Mark an open alert resolved; returns it (to announce) or None if it wasn't open."""
    return _query(
        "UPDATE ops_alerts SET resolved_at=now() WHERE key=%s AND resolved_at IS NULL RETURNING key, title",
        (key,),
        fetch="one",
        commit=True,
    )


def ops_alerts_open() -> list[dict]:
    return _query("SELECT * FROM ops_alerts WHERE resolved_at IS NULL ORDER BY first_fired_at") or []


# ── Onboarding (UX-01) ───────────────────────────────────────────────────────


def user_onboarding(user_id: int, today: "date") -> dict:
    """What the first-run card needs: whether it was dismissed, free trial
    messages left today, and whether a BYO key / Telegram are set up."""
    row = _query(
        "SELECT u.onboarded_at, u.free_msgs_used_today, u.free_msgs_reset_at, u.telegram_chat_id, "
        "  EXISTS (SELECT 1 FROM user_llm_config c WHERE c.user_id = u.id) AS byo "
        "FROM users u WHERE u.id = %s",
        (user_id,),
        fetch="one",
    )
    if not row:
        return {"onboarded": True, "used_today": 0, "byo": False, "telegram": False}
    used = int(row["free_msgs_used_today"] or 0) if row["free_msgs_reset_at"] == today else 0
    return {
        "onboarded": row["onboarded_at"] is not None,
        "used_today": used,
        "byo": bool(row["byo"]),
        "telegram": bool(row["telegram_chat_id"]),
    }


def user_mark_onboarded(user_id: int) -> None:
    _query(
        "UPDATE users SET onboarded_at = now() WHERE id = %s AND onboarded_at IS NULL",
        (user_id,),
        fetch=None,
        commit=True,
    )
