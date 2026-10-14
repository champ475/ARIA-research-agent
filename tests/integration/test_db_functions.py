"""TEST-02: every public utils/db.py function runs against a real Postgres.

User-scoped functions are exercised by test_tenant_isolation.py (SEC-13);
this file covers the rest (shared caches, auth rate limiting, cross-tenant
checker reads) and the concurrency races a single-threaded unit test can't
see. A meta-test fails when a public db function has no integration test.
"""

import inspect
import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.integration.test_tenant_isolation import COVERED as ISOLATION_COVERED

pytestmark = pytest.mark.integration


def _concurrently(n: int, fn):
    """Run fn(i) on n threads released at the same instant; return results."""
    barrier = threading.Barrier(n)
    results: list = [None] * n
    errors: list = []

    def worker(i):
        try:
            barrier.wait()
            results[i] = fn(i)
        except Exception as e:  # surfaced below, not swallowed
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return results


# ── shared caches ─────────────────────────────────────────────────────────────


def test_pdf_cache_upserts_period_less_documents(db):
    db.pdf_cache_set("tcs", "annual_report", "v1", url="u1")
    db.pdf_cache_set("TCS", "annual_report", "v2", url="u2")  # refetch must replace, not duplicate
    assert db.pdf_cache_get("TCS", "annual_report") == "v2"
    rows = db._query("SELECT count(*) AS n FROM pdf_cache WHERE symbol='TCS' AND doc_type='annual_report'")
    assert rows[0]["n"] == 1


def test_pdf_cache_prefers_exact_period_then_falls_back(db):
    db.pdf_cache_set("INFY", "quarterly_results", "generic")
    db.pdf_cache_set("INFY", "quarterly_results", "q2 text", period="Q2FY27")
    assert db.pdf_cache_get("INFY", "quarterly_results", "Q2FY27") == "q2 text"
    assert db.pdf_cache_get("INFY", "quarterly_results", "Q3FY27") == "generic"
    assert db.pdf_cache_get("INFY", "quarterly_results") == "generic"
    assert db.pdf_cache_get("INFY", "annual_report") is None


def test_evidence_cache_put_upserts_and_get_round_trips(db):
    assert db.evidence_cache_get("fetch_quote", "h1") is None
    db.evidence_cache_put("fetch_quote", "h1", "TCS", "quote_info", {"price": 4000.5}, 0.9)
    db.evidence_cache_put("fetch_quote", "h1", "TCS", "quote_info", {"price": 4010.0}, 1.0)
    row = db.evidence_cache_get("fetch_quote", "h1")
    assert row["payload"] == {"price": 4010.0} and row["confidence"] == pytest.approx(1.0)
    n = db._query("SELECT count(*) AS n FROM evidence_cache WHERE tool='fetch_quote'")[0]["n"]
    assert n == 1


# ── checker-side cross-tenant read ────────────────────────────────────────────


def test_alert_get_active_all_spans_users_and_skips_inactive(db, two_users):
    a, b = two_users
    a1 = db.alert_add(a, "TCS", "price_above", "above", 4000.0)
    b1 = db.alert_add(b, "INFY", "price_below", "below", 1500.0)
    paused = db.alert_add(b, "SBIN", "price_above", "above", 900.0)
    db.alert_pause(b, paused)
    assert [r["id"] for r in db.alert_get_active_all()] == [a1, b1]


# ── auth rate limiting ────────────────────────────────────────────────────────


def test_auth_attempts_count_window_and_clear(db):
    since = datetime.now(UTC) - timedelta(minutes=5)
    for _ in range(3):
        db.auth_attempt_record("login:a@example.com")
    db.auth_attempt_record("login:b@example.com")
    assert db.auth_attempt_count("login:a@example.com", since) == 3
    assert db.auth_attempt_count("login:a@example.com", datetime.now(UTC) + timedelta(minutes=1)) == 0
    db.auth_attempt_clear("login:a@example.com")
    assert db.auth_attempt_count("login:a@example.com", since) == 0
    assert db.auth_attempt_count("login:b@example.com", since) == 1


# ── Telegram link codes ───────────────────────────────────────────────────────


def test_link_code_expired_is_rejected_and_consumed(db, two_users):
    a, _ = two_users
    db.telegram_link_code_create(a, "OLD", datetime.now(UTC) - timedelta(seconds=1))
    assert db.telegram_link_code_resolve("OLD") is None
    assert db._query("SELECT count(*) AS n FROM telegram_link_codes")[0]["n"] == 0


def test_link_code_race_links_exactly_once(db, two_users):
    a, _ = two_users
    db.telegram_link_code_create(a, "RACE", datetime.now(UTC) + timedelta(minutes=10))
    results = _concurrently(10, lambda i: db.telegram_link_code_resolve("RACE"))
    assert results.count(a) == 1 and results.count(None) == 9


# ── SEC-12: free-trial quota under concurrency ────────────────────────────────


def test_trial_quota_race_never_exceeds_the_limit(db, two_users):
    a, _ = two_users
    today = date(2026, 10, 14)
    allowed = _concurrently(20, lambda i: db.trial_quota_check_and_increment(a, 5, today))
    assert allowed.count(True) == 5
    used = db._query("SELECT free_msgs_used_today AS n FROM users WHERE id=%s", (a,))[0]["n"]
    assert used == 5


def test_trial_quota_resets_on_a_new_day(db, two_users):
    a, _ = two_users
    d1, d2 = date(2026, 10, 14), date(2026, 10, 15)
    assert [db.trial_quota_check_and_increment(a, 2, d1) for _ in range(3)] == [True, True, False]
    assert db.trial_quota_check_and_increment(a, 2, d2) is True
    assert db.trial_quota_check_and_increment(a, 0, d2) is False
    assert db.trial_quota_check_and_increment(999_999, 2, d2) is False  # no such user


# ── coverage meta-test ────────────────────────────────────────────────────────

# Public functions covered in this file (user-scoped ones are in ISOLATION_COVERED).
COVERED = {
    "pdf_cache_get": test_pdf_cache_prefers_exact_period_then_falls_back,
    "pdf_cache_set": test_pdf_cache_upserts_period_less_documents,
    "evidence_cache_get": test_evidence_cache_put_upserts_and_get_round_trips,
    "evidence_cache_put": test_evidence_cache_put_upserts_and_get_round_trips,
    "alert_get_active_all": test_alert_get_active_all_spans_users_and_skips_inactive,
    "auth_attempt_record": test_auth_attempts_count_window_and_clear,
    "auth_attempt_count": test_auth_attempts_count_window_and_clear,
    "auth_attempt_clear": test_auth_attempts_count_window_and_clear,
    "telegram_link_code_resolve": test_link_code_race_links_exactly_once,
}
# Exercised by every other test (pool plumbing) or by test_migrations.py.
INFRASTRUCTURE = {"get_conn", "release_conn", "ensure_schema"}


def test_every_public_db_function_has_an_integration_test():
    import utils.db as db_module

    public = {
        name
        for name, fn in inspect.getmembers(db_module, inspect.isfunction)
        if fn.__module__ == db_module.__name__ and not name.startswith("_")
    }
    missing = sorted(public - set(COVERED) - set(ISOLATION_COVERED) - INFRASTRUCTURE)
    assert missing == [], f"utils/db.py functions without an integration test: {missing}"
