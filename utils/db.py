"""
utils/db.py — Postgres (Neon) query helpers.
Single shared DB: read/written by the local Gradio app AND by
scripts/check_alerts.py (run on a schedule via GitHub Actions).
The schema itself lives in Alembic migrations (migrations/versions/, ARCH-01).
"""

import contextlib
import json
import logging
import os
import threading
from datetime import UTC, date

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
            with contextlib.suppress(Exception):
                conn.close()
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


def _query(sql: str, params: tuple | dict = (), fetch: str | None = "all", commit: bool = False):
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


# ── Portfolio helpers ─────────────────────────────────────────────────────────


def portfolio_add(user_id: int, symbol: str, qty: float, avg_price: float):
    _query(
        "INSERT INTO portfolio (user_id, symbol, qty, avg_price) VALUES (%s,%s,%s,%s)",
        (user_id, symbol.upper(), qty, avg_price),
        fetch=None,
        commit=True,
    )


def portfolio_remove(user_id: int, symbol: str):
    _query(
        "DELETE FROM portfolio WHERE user_id=%s AND symbol=%s",
        (user_id, symbol.upper()),
        fetch=None,
        commit=True,
    )


def portfolio_all(user_id: int) -> list[dict]:
    return _query("SELECT * FROM portfolio WHERE user_id=%s", (user_id,), fetch="all")


# ── Watchlist helpers ─────────────────────────────────────────────────────────


def watchlist_add(user_id: int, symbol: str):
    _query(
        "INSERT INTO watchlist (user_id, symbol) VALUES (%s,%s) ON CONFLICT (user_id, symbol) DO NOTHING",
        (user_id, symbol.upper()),
        fetch=None,
        commit=True,
    )


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


def chat_save(user_id: int, session_id: int, tab: str, role: str, content: str):
    """Appends a message to a session — only if `user_id` owns that session
    (SEC-13: a forged session id can't write into someone else's history)."""
    _query(
        "INSERT INTO chat_history (user_id, session_id, tab, role, content) "
        "SELECT %s, %s, %s, %s, %s "
        "WHERE EXISTS (SELECT 1 FROM chat_sessions WHERE id=%s AND user_id=%s)",
        (user_id, session_id, tab, role, content, session_id, user_id),
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
    except psycopg2.errors.UniqueViolation as e:
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
    except psycopg2.errors.UniqueViolation as e:
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


# ── Free-trial quota helpers (agents/graph_orchestrator.py) ──────────────────


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


def accept_terms(user_id: int) -> None:
    _query("UPDATE users SET accepted_terms_at=now() WHERE id=%s", (user_id,), fetch=None, commit=True)


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
) -> None:
    _query(
        "INSERT INTO turns (turn_id, user_id, session_id, intent, complexity, status, error_id, latency_ms, "
        "ttft_ms, llm_calls, tokens_in, tokens_out, tokens_estimated, providers, prompt_versions, tool_calls, "
        "cache_hits, node_ms, data_gaps, grounding) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
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
