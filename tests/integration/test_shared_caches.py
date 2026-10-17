"""DATA-10 on a real Postgres: the OHLCV cache and bhavcopy history live in
the database (they survive losing the disk), and the retention job purges
what is past its period but keeps what is fresh or awaited."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from tools.data import bhavcopy_history as bh
from tools.data import cache
from utils import retention

pytestmark = pytest.mark.integration


@pytest.fixture
def empty_disk(tmp_path, monkeypatch):
    """Point both disk fallbacks at an empty directory: a fresh container."""
    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path / "parquet"))
    monkeypatch.setattr(bh, "_HISTORY_DIR", str(tmp_path / "bhav"))
    return tmp_path


def _frame():
    return pd.DataFrame(
        {"Open": [1.0, 2.0], "High": [2.0, 3.0], "Low": [0.5, 1.5], "Close": [1.5, 2.5], "Volume": [10, 20]},
        index=pd.DatetimeIndex(["2026-10-15", "2026-10-16"]),
    )


def test_ohlcv_cache_round_trips_through_postgres_without_disk(db, empty_disk):
    cache.cache_write("TCS", "1y", "1d", _frame())
    assert not (empty_disk / "parquet").exists()  # nothing written to local disk
    got = cache.cache_read("TCS", "1y", "1d")
    assert list(got["Close"]) == [1.5, 2.5]
    assert cache.cache_read("TCS", "6mo", "1d") is None


def test_stale_ohlcv_entry_is_a_miss(db, empty_disk):
    cache.cache_write("TCS", "1y", "1d", _frame())
    db._query("UPDATE ohlcv_cache SET fetched_at = now() - interval '5 hours'", fetch=None, commit=True)
    assert cache.cache_read("TCS", "1y", "1d", max_age_hours=4) is None
    assert cache.cache_read("TCS", "1y", "1d", max_age_hours=6) is not None


def test_bhavcopy_fallback_survives_a_container_restart(db, empty_disk, monkeypatch):
    """Record sessions, 'restart' with an empty disk, and the window is still there."""
    monkeypatch.setattr("tools.data.adjustments.corporate_factors", lambda s: [])
    for i, day in enumerate(["14-Oct-2026", "15-Oct-2026", "16-Oct-2026"]):
        bh.record_row(
            "ITC", {"date": day, "open": 400 + i, "high": 410 + i, "low": 395 + i, "close": 405 + i}
        )
    bh.record_row(
        "ITC", {"date": "16-Oct-2026", "open": 1, "high": 1, "low": 1, "close": 1}
    )  # repeat day: ignored
    hist = bh.read_history("ITC")
    assert list(hist["Close"]) == [405, 406, 407]
    assert not (empty_disk / "bhav").exists()


def test_bhavcopy_keeps_only_the_newest_300_sessions(db, empty_disk):
    base = date(2025, 1, 1)
    for i in range(305):
        db.bhavcopy_bar_put("INFY", base + timedelta(days=i), 1, 1, 1, i)
    bars = db.bhavcopy_bars_get("INFY")
    assert len(bars) == db.BHAVCOPY_MAX_BARS
    assert bars[-1]["close"] == 304


def _seed_old_and_new(db):
    db._query(
        "INSERT INTO evidence_cache (tool, args_hash, entity, evidence_type, payload, fetched_at) VALUES "
        "('t','old','X','quote_info','{}', now() - interval '10 days'), ('t','new','X','quote_info','{}', now())",
        fetch=None,
        commit=True,
    )
    db._query(
        "INSERT INTO pdf_cache (symbol, doc_type, period, fetched_at) VALUES "
        "('A','ar','', now() - interval '40 days'), ('B','ar','', now())",
        fetch=None,
        commit=True,
    )
    db._query(
        "INSERT INTO ohlcv_cache (symbol, period, interval, data, fetched_at) VALUES "
        "('A','1y','1d','x', now() - interval '9 days'), ('B','1y','1d','x', now())",
        fetch=None,
        commit=True,
    )


def test_retention_purges_old_cache_rows_and_keeps_fresh_ones(db):
    _seed_old_and_new(db)
    counts = retention.run_retention()
    assert counts["evidence_cache"] == 1 and counts["pdf_cache"] == 1 and counts["ohlcv_cache"] == 1
    assert [r["args_hash"] for r in db._query("SELECT args_hash FROM evidence_cache")] == ["new"]
    assert [r["symbol"] for r in db._query("SELECT symbol FROM pdf_cache")] == ["B"]
    assert all(r["ok"] for r in retention.report())
    assert retention.run_retention()["evidence_cache"] == 0  # idempotent


def _checkpoint(db, thread: str, age_days: int):
    ts = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    db._query(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
        "VALUES (%s, '', %s, 'msgpack', %s, '{}')",
        (thread, f"{thread}-1", json.dumps({"ts": ts, "id": f"{thread}-1"})),
        fetch=None,
        commit=True,
    )
    db._query(
        "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob) "
        "VALUES (%s, '', %s, 'task', 0, 'c', 'msgpack', 'x')",
        (thread, f"{thread}-1"),
        fetch=None,
        commit=True,
    )


def test_checkpoints_older_than_7_days_go_but_a_paused_thread_stays(db, two_users):
    a, _ = two_users
    for t in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        db._query(f"DELETE FROM {t}", fetch=None, commit=True)
    _checkpoint(db, "user-1-old", 10)
    _checkpoint(db, "user-1-paused", 10)
    _checkpoint(db, "user-1-fresh", 1)
    db._query(
        "INSERT INTO chat_sessions (user_id, tab, pending_thread_id) VALUES (%s,'chat','user-1-paused')",
        (a,),
        fetch=None,
        commit=True,
    )
    counts = retention.run_retention()
    assert counts["langgraph_checkpoints"] == 1
    left = {r["thread_id"] for r in db._query("SELECT thread_id FROM checkpoints")}
    assert left == {"user-1-paused", "user-1-fresh"}
    assert {r["thread_id"] for r in db._query("SELECT thread_id FROM checkpoint_writes")} == left


def test_purge_refuses_tables_outside_the_schedule(db):
    with pytest.raises(ValueError):
        db.purge_older_than("users", "created_at", datetime.now(UTC))
