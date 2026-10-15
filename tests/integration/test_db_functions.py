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

from tests.integration import test_audit, test_sessions
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


# ── OBS-02 turns ──────────────────────────────────────────────────────────────


def _turn_kwargs(turn_id, user_id, **over):
    base = dict(
        turn_id=turn_id,
        user_id=user_id,
        session_id="12",
        intent="equity",
        complexity="moderate",
        status="ok",
        error_id=turn_id.replace("-", "")[:8],
        latency_ms=1234,
        ttft_ms=456,
        llm_calls=3,
        tokens_in=900,
        tokens_out=120,
        tokens_estimated=False,
        providers=["Groq"],
        prompt_versions={"ab12cd34": 3},
        tool_calls=4,
        cache_hits=1,
        node_ms={"Planner": 300},
        data_gaps=0,
        grounding={"unsupported": []},
    )
    base.update(over)
    return base


def test_turn_record_round_trip_and_error_id_lookup(db, two_users):
    import uuid

    a, _ = two_users
    tid = uuid.uuid4().hex
    db.turn_record(**_turn_kwargs(tid, a))
    db.turn_record(**_turn_kwargs(tid, a, status="error"))  # replay is ignored, not duplicated
    (row,) = db.turns_by_error_id(tid[:8].upper())
    assert row["status"] == "ok" and row["providers"] == ["Groq"]
    assert row["prompt_versions"] == {"ab12cd34": 3} and row["grounding"] == {"unsupported": []}
    db.turn_set_trace_url(tid, "https://smith.example/r/1")
    assert db.turns_by_error_id(tid[:8])[0]["trace_url"] == "https://smith.example/r/1"
    assert db.turns_by_error_id("ffffffff") == []


def test_turns_are_deleted_with_their_user(db, two_users):
    import uuid

    a, _ = two_users
    db.turn_record(**_turn_kwargs(uuid.uuid4().hex, a))
    db._query("DELETE FROM users WHERE id=%s", (a,), fetch=None, commit=True)
    assert db._query("SELECT count(*) AS n FROM turns")[0]["n"] == 0


# ── VAL-04: alert uniqueness and cap ──────────────────────────────────────────


def test_duplicate_active_alert_is_rejected_but_other_users_and_specs_are_fine(db, two_users):
    a, b = two_users
    first = db.alert_add(a, "TCS", "pct_change", "c", 5.0, direction="down")
    with pytest.raises(db.DuplicateAlert) as e:
        db.alert_add(a, "TCS", "pct_change", "c", 5.0, direction="down")
    assert e.value.existing_id == first
    db.alert_add(a, "TCS", "pct_change", "c", 5.0, direction="up")  # different direction
    db.alert_add(b, "TCS", "pct_change", "c", 5.0, direction="down")  # different user
    db.alert_pause(a, first)
    again = db.alert_add(a, "TCS", "pct_change", "c", 5.0, direction="down")  # original paused
    with pytest.raises(db.DuplicateAlert):
        db.alert_resume(a, first)
    assert again != first


def test_fifty_active_alerts_is_the_cap(db, two_users):
    a, _ = two_users
    for i in range(db.MAX_ACTIVE_ALERTS):
        db.alert_add(a, "TCS", "price_above", "c", 1000.0 + i)
    with pytest.raises(db.AlertLimitReached):
        db.alert_add(a, "TCS", "price_above", "c", 9999.0)
    db.alert_pause(a, db.alert_list(a)[0]["id"])
    db.alert_add(a, "TCS", "price_above", "c", 9999.0)  # room again after pausing one


# ── DATA-04: mutual fund metrics cache ────────────────────────────────────────


def test_mf_metrics_cache_round_trip_and_freshness(db):
    from datetime import date, timedelta

    row = {
        "scheme_code": "119544",
        "scheme_name": "Alpha ELSS",
        "category": "ELSS",
        "nav": 65.55,
        "nav_date": date(2026, 10, 13),
        "history_start": date(2013, 1, 1),
        "cagr_1y": -4.57,
        "cagr_3y": 9.51,
        "cagr_5y": 6.9,
        "volatility_1y": 14.2,
        "max_drawdown_3y": -18.0,
        "consistency_1y": 80.0,
    }
    db.mf_metrics_put([row])
    db.mf_metrics_put([{**row, "cagr_1y": -4.0}])  # upsert, not a second row
    got = db.mf_metrics_get(["119544", "999"], date.today() - timedelta(days=1))
    assert list(got) == ["119544"] and got["119544"]["cagr_1y"] == -4.0
    assert db.mf_metrics_get(["119544"], date.today() + timedelta(days=1)) == {}  # stale -> recompute
    assert db.mf_metrics_get([], date.today()) == {}


def test_secret_key_rotation_rewraps_every_stored_key(db, two_users, monkeypatch):
    """SEC-11: rotate_secret_key re-encrypts rows with the new key; a row
    changed after it was read is left alone (compare-and-set)."""
    import config
    import scripts.rotate_secret_key as rot
    from utils import crypto

    a, b = two_users
    old_key, new_key = "o" * 40, "n" * 40
    monkeypatch.setattr(config, "SECRET_KEY", old_key)
    monkeypatch.setattr(config, "SECRET_KEY_PREVIOUS", "")
    crypto.reset_cache()
    db.llm_config_set(a, "openai_compatible", "m", crypto.encrypt("key-a"))
    db.llm_config_set(b, "openai_compatible", "m", crypto.encrypt("key-b"))
    assert {r["user_id"] for r in db.llm_config_ciphertexts()} == {a, b}

    monkeypatch.setattr(config, "SECRET_KEY", new_key)
    monkeypatch.setattr(config, "SECRET_KEY_PREVIOUS", old_key)
    crypto.reset_cache()
    assert rot.rotate_all() == {"rows": 2, "rotated": 2, "unreadable": 0, "changed_meanwhile": 0}
    assert db.llm_config_rewrap(a, "stale-ciphertext", "x") is False

    monkeypatch.setattr(config, "SECRET_KEY_PREVIOUS", "")
    crypto.reset_cache()
    assert crypto.decrypt(db.llm_config_get(a)["api_key_encrypted"]) == "key-a"
    assert crypto.decrypt(db.llm_config_get(b)["api_key_encrypted"]) == "key-b"
    crypto.reset_cache()


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
    "turn_set_trace_url": test_turn_record_round_trip_and_error_id_lookup,
    "turns_by_error_id": test_turn_record_round_trip_and_error_id_lookup,
    "mf_metrics_get": test_mf_metrics_cache_round_trip_and_freshness,
    "mf_metrics_put": test_mf_metrics_cache_round_trip_and_freshness,
    "llm_config_ciphertexts": test_secret_key_rotation_rewraps_every_stored_key,
    # SEC-04 session plumbing, exercised end to end in test_sessions.py:
    "auth_session_lookup": test_sessions.test_expired_session_gets_no_access,
    "auth_session_touch": test_sessions.test_refresh_keeps_you_logged_in_and_gradio_routes_need_the_cookie,
    "auth_session_delete": test_sessions.test_logout_makes_the_token_unusable,
    "auth_sessions_purge_expired": test_sessions.test_expired_session_gets_no_access,
    "audit_log_write": test_audit.test_audit_rows_cannot_be_changed_or_deleted,
    "audit_log_for": test_audit.test_alert_and_screener_writes_are_audited,
}
# Exercised by every other test (pool plumbing) or by test_migrations.py.
# Pool plumbing is exercised by every test and directly in test_db_pool.py.
INFRASTRUCTURE = {
    "get_conn",
    "release_conn",
    "ensure_schema",
    "close_pool",
    "connection_kwargs",
    "pool_stats",
}


def test_every_public_db_function_has_an_integration_test():
    import utils.db as db_module

    public = {
        name
        for name, fn in inspect.getmembers(db_module, inspect.isfunction)
        if fn.__module__ == db_module.__name__ and not name.startswith("_")
    }
    missing = sorted(public - set(COVERED) - set(ISOLATION_COVERED) - INFRASTRUCTURE)
    assert missing == [], f"utils/db.py functions without an integration test: {missing}"
