"""REL-04 on a real Postgres: one psycopg 3 pool that waits for a free
connection, times out cleanly, survives discarded connections, survives
concurrency, and cancels runaway statements server-side."""

import threading

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def tiny_pool(pg_db, monkeypatch):
    """utils.db with a one-connection pool and a short acquire timeout."""
    db = pg_db
    db.close_pool()
    monkeypatch.setattr(db, "DB_POOL_MAX", 1)
    monkeypatch.setattr(db, "DB_POOL_MIN", 1)
    monkeypatch.setattr(db, "DB_POOL_ACQUIRE_TIMEOUT", 0.5)
    yield db
    db.close_pool()


def test_get_conn_waits_for_a_free_connection_instead_of_failing(tiny_pool):
    first = tiny_pool.get_conn()
    threading.Timer(0.1, lambda: tiny_pool.release_conn(first)).start()
    second = tiny_pool.get_conn()  # blocks ~0.1s, then succeeds
    tiny_pool.release_conn(second)


def test_get_conn_times_out_cleanly_when_pool_stays_full(tiny_pool):
    held = tiny_pool.get_conn()
    with pytest.raises(tiny_pool.PoolTimeout):
        tiny_pool.get_conn()
    tiny_pool.release_conn(held)
    tiny_pool.release_conn(tiny_pool.get_conn())  # the slot was not leaked by the timeout


def test_discarded_connection_is_replaced(tiny_pool):
    tiny_pool.release_conn(tiny_pool.get_conn(), discard=True)
    conn = tiny_pool.get_conn()
    assert not conn.closed
    tiny_pool.release_conn(conn)
    assert tiny_pool._query("SELECT 1 AS one", fetch="one") == {"one": 1}


def test_aborted_transaction_is_rolled_back_before_reuse(tiny_pool):
    conn = tiny_pool.get_conn()
    with pytest.raises(Exception):  # noqa: B017 - any SQL error aborts the transaction
        conn.execute("SELECT * FROM no_such_table")
    tiny_pool.release_conn(conn)
    assert tiny_pool._query("SELECT 2 AS two", fetch="one") == {"two": 2}


def test_statement_timeout_cancels_a_slow_query(pg_db, monkeypatch):
    import psycopg

    pg_db.close_pool()
    monkeypatch.setattr(pg_db, "DB_STATEMENT_TIMEOUT_MS", 300)
    try:
        with pytest.raises(psycopg.errors.QueryCanceled):
            pg_db._query("SELECT pg_sleep(3)", fetch="one")
        assert pg_db._query("SELECT 3 AS three", fetch="one") == {"three": 3}
    finally:
        pg_db.close_pool()


def test_concurrent_queries_share_the_pool_without_errors(pg_db):
    errors: list[BaseException] = []

    def work():
        try:
            for _ in range(5):
                assert pg_db._query("SELECT pg_sleep(0.01), 1 AS one", fetch="one")["one"] == 1
        except BaseException as e:  # noqa: BLE001 - collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    stats = pg_db.pool_stats()
    assert stats["pool_size"] <= stats["pool_max"]


def test_only_one_postgres_driver_is_locked():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for lock in ("requirements.lock", "requirements-dev.lock"):
        text = (root / lock).read_text(encoding="utf-8").lower()
        assert "psycopg2" not in text, f"{lock} still pins psycopg2"
