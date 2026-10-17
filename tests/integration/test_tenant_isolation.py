"""SEC-13 — tenant isolation on a real Postgres.

User B acts on user A's ids and rows through every user-scoped function in
utils/db.py and every user-scoped graph tool; nothing of A's may leak to B or
change. COVERED maps each user-scoped db function to the test that proves it,
and test_every_user_scoped_db_function_is_covered fails if a new function
taking `user_id` is added without an isolation test."""

import inspect
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


# ── utils/db.py ───────────────────────────────────────────────────────────────


def test_onboarding_is_per_user(db, two_users):
    """UX-01: dismissing the card is one user's choice."""
    from datetime import date

    a, b = two_users
    db._query("UPDATE users SET onboarded_at = NULL", fetch=None, commit=True)
    db.user_mark_onboarded(a)
    assert db.user_onboarding(a, date.today())["onboarded"] is True
    assert db.user_onboarding(b, date.today())["onboarded"] is False


def test_downloads_are_per_user(db, two_users):
    """OPS-02: a stored download is readable only by its owner."""
    a, b = two_users
    token = db.download_put(a, "r.csv", "text/csv", b"a,b")
    assert db.download_get(b, token) is None
    assert db.download_get(a, token)["filename"] == "r.csv"


def test_portfolio_lots_are_per_user(db, two_users):
    """UX-05: B can't edit, remove or replace A's lots."""
    a, b = two_users
    lot = db.portfolio_add(a, "TCS", 10, 3500.0)
    assert not db.portfolio_update(b, lot, 1, 1.0)
    assert not db.portfolio_remove_lot(b, lot)
    db.portfolio_import(b, [{"symbol": "ITC", "qty": 1, "avg_price": 1}], replace=True)
    assert [r["symbol"] for r in db.portfolio_all(a)] == ["TCS"]
    assert [r["symbol"] for r in db.portfolio_all(b)] == ["ITC"]


def test_portfolio(db, two_users):
    a, b = two_users
    db.portfolio_add(a, "TCS", 10, 3500.0)
    assert db.portfolio_all(b) == []
    db.portfolio_remove(b, "TCS")
    assert len(db.portfolio_all(a)) == 1


def test_watchlist(db, two_users):
    a, b = two_users
    db.watchlist_add(a, "TCS")
    db.watchlist_add(b, "INFY")
    assert db.watchlist_all(b) == ["INFY"]
    db.watchlist_remove(b, "TCS")
    assert db.watchlist_all(a) == ["TCS"]


def test_sessions_and_chat_history(db, two_users):
    a, b = two_users
    sid = db.session_create(a, "chat", title="A's secret plan")
    db.chat_save(a, sid, "chat", "user", "my portfolio is 50L")
    assert db.session_list(b, "chat") == []
    db.session_touch(b, sid, title="hijacked")
    assert db.session_list(a, "chat")[0]["title"] == "A's secret plan"
    db.chat_save(b, sid, "chat", "user", "injected by B")
    assert [m["content"] for m in db.chat_load(a, sid)] == ["my portfolio is 50L"]
    assert db.chat_load(b, sid) == []
    db.session_delete(b, sid)
    assert len(db.session_list(a, "chat")) == 1
    assert db.session_rename(b, sid, "hijacked") is False
    assert db.session_list(a, "chat")[0]["title"] == "A's secret plan"
    assert db.session_search(b, "chat", ["portfolio"]) == []
    assert db.session_get_title(b, sid) is None and db.session_get_title(a, sid) == "A's secret plan"
    assert [x["id"] for x in db.session_search(a, "chat", ["portfolio"])] == [sid]


def test_owner_can_still_touch_and_save(db, two_users):
    a, _ = two_users
    sid = db.session_create(a, "chat")
    db.session_touch(a, sid, title="renamed")
    db.chat_save(a, sid, "chat", "assistant", "hello")
    assert db.session_list(a, "chat")[0]["title"] == "renamed"
    assert len(db.chat_load(a, sid)) == 1


def test_reports(db, two_users):
    a, b = two_users
    rid = db.report_save(a, "TCS", "initiating_coverage", "TCS IC", "content")
    assert db.report_list(b) == []
    assert db.report_get(b, rid) is None
    assert db.report_get(a, rid)["content"] == "content"


def test_saved_screeners(db, two_users):
    a, b = two_users
    db.screener_save(a, "value", {"pe_max": 15})
    assert db.screener_list(b) == []
    assert len(db.screener_list(a)) == 1


def test_alerts(db, two_users):
    a, b = two_users
    aid = db.alert_add(a, "TCS", "price_above", "Price > 4000", 4000.0)
    assert db.alert_list(b) == []
    db.alert_pause(b, aid)
    assert db.alert_list(a)[0]["active"] is True
    db.alert_remove(b, aid)
    db.alert_pause(a, aid)
    db.alert_resume(b, aid)
    assert db.alert_list(a)[0]["active"] is False
    assert len(db.alert_list(a)) == 1
    assert db.alert_get(b, aid) is None and db.alert_get(a, aid)["id"] == aid
    assert db.alert_update(b, aid, "x", 1.0, None, False, 24) is False
    assert db.alert_get(a, aid)["threshold"] == 4000


def test_admin_console_scope(db, two_users):
    """OBS-05: these functions cross tenants on purpose; services/admin.py only calls them
    for an admin (tests/integration/test_admin_console.py proves a non-admin is refused)."""
    a, b = two_users
    assert db.user_is_admin(a) is False
    db.user_set_admin(a, True)
    assert db.user_is_admin(a) is True and db.user_is_admin(b) is False
    assert (
        db.turns_recent(a) == []
        and db.user_set_disabled(b, True) is True
        and db.user_set_disabled(b, False) is True
    )


def test_notifications(db, two_users):
    a, b = two_users
    nid = db.notification_add(a, "TCS above 4000")
    assert db.notifications_list(b) == []
    assert db.notifications_unread_count(b) == 0
    db.notifications_mark_read(b, [nid])
    db.notifications_mark_read(b)
    assert db.notifications_unread_count(a) == 1
    db.notifications_mark_read(a, [nid])
    assert db.notifications_unread_count(a) == 0


def test_financial_profile(db, two_users):
    a, b = two_users
    db.profile_upsert(a, {"age": 30, "corpus": 5_000_000})
    assert db.profile_get(b) == {}
    db.profile_upsert(b, {"age": 50})
    db.profile_clear(b)
    assert db.profile_get(a) == {"age": 30, "corpus": 5_000_000}


def test_llm_config(db, two_users):
    a, b = two_users
    db.llm_config_set(
        a, "openai_compatible", "gpt-4o-mini", "enc-key-a", base_url="https://api.openai.com/v1"
    )
    assert db.llm_config_get(b) is None
    db.llm_config_clear(b)
    assert db.llm_config_get(a)["api_key_encrypted"] == "enc-key-a"
    # Key rotation's compare-and-set only ever touches the named user's row.
    assert db.llm_config_rewrap(b, "enc-key-a", "enc-by-b") is False
    assert db.llm_config_get(a)["api_key_encrypted"] == "enc-key-a"


def test_auth_sessions(db, two_users):
    """SEC-04: B listing or revoking sessions never sees or touches A's."""
    from datetime import UTC, datetime, timedelta

    a, b = two_users
    later = datetime.now(UTC) + timedelta(days=1)
    db.auth_session_create(a, "hash-a", later, ip="1.2.3.4", user_agent="A's browser")
    db.auth_session_create(b, "hash-b", later)
    assert [r["user_agent"] for r in db.auth_sessions_list(b)] == [None]
    assert db.auth_sessions_revoke_others(b, "hash-b") == 0
    assert db.auth_session_lookup("hash-a")["user_id"] == a
    assert len(db.auth_sessions_list(a)) == 1


def test_account_lifecycle_functions(db, two_users):
    """SEC-06: tokens, password and deletion act only on the named user."""
    from datetime import UTC, datetime, timedelta

    a, b = two_users
    later = datetime.now(UTC) + timedelta(minutes=30)
    db.auth_token_create(a, "reset", "tok-a", later)
    db.auth_token_create(b, "reset", "tok-b", later)  # B's new token doesn't void A's
    assert db.auth_token_consume("reset", "tok-a") == a
    a_hash = db.user_password_hash(a)
    db.user_set_password(b, "new-hash-b")
    db.user_rehash_password(b, "new-hash-b")
    assert db.user_password_hash(a) == a_hash and db.user_password_hash(b) == "new-hash-b"
    db._query("UPDATE users SET email_verified_at=NULL", fetch=None, commit=True)
    db.user_mark_verified(b)
    assert db.user_by_email(db.user_email(a))["email_verified_at"] is None
    db.watchlist_add(a, "TCS")
    assert db.user_delete(b) is True
    assert db.user_email(b) is None and db.watchlist_all(a) == ["TCS"]


def test_data_export_rows(db, two_users):
    """COMP-03: export helpers return only the named user's rows."""
    a, b = two_users
    db.watchlist_add(a, "TCS")
    assert db.user_rows("watchlist", b) == []
    assert db.user_account_row(b)["id"] == b


def test_session_pending(db, two_users):
    """VAL-10: a paused confirmation is readable and clearable only by its owner."""
    a, b = two_users
    sid = db.session_create(a, "chat", title="A")
    db.session_pending_set(a, sid, "user-a-thread")
    assert db.session_pending_get(b, sid) is None
    db.session_pending_set(b, sid, None)  # B can't clear A's pending step
    assert db.session_pending_get(a, sid) == "user-a-thread"
    db.session_pending_set(a, sid, None)
    assert db.session_pending_get(a, sid) is None


def test_trial_tokens(db, two_users):
    """LLM-11: each user's daily trial token budget is their own and resets daily."""
    a, b = two_users
    day1, day2 = date(2026, 10, 16), date(2026, 10, 17)
    assert db.trial_tokens_add(a, 900, day1) == 900
    assert db.trial_tokens_add(a, 100, day1) == 1000
    assert db.trial_tokens_used(b, day1) == 0
    assert db.trial_tokens_used(a, day2) == 0  # a new day starts at zero
    assert db.trial_tokens_add(a, 5, day2) == 5


def test_feedback(db, two_users):
    """LLM-12: B's ratings never land on or reveal A's."""
    a, b = two_users
    db.feedback_record(a, "turn-a", -1, reason="wrong number")
    assert db.feedback_for(b) == []
    db.feedback_record(b, "turn-a", 1)  # B rating the same turn id is B's own row
    assert db.feedback_for(a)[0]["rating"] == -1


def test_trial_quota(db, two_users):
    a, b = two_users
    today = date(2026, 10, 14)
    assert db.trial_quota_check_and_increment(a, 1, today) is True
    assert db.trial_quota_check_and_increment(a, 1, today) is False
    assert db.trial_quota_check_and_increment(b, 1, today) is True


def test_telegram_linking(db, two_users):
    a, b = two_users
    db.telegram_link_code_create(a, "CODE-A", datetime.now(UTC) + timedelta(minutes=10))
    db.telegram_chat_id_set(a, "111")
    assert db.telegram_chat_id_get(b) is None
    assert db.telegram_link_code_resolve("CODE-A") == a
    assert db.telegram_link_code_resolve("CODE-A") is None  # single use
    db.telegram_chat_id_set(b, "222")
    assert db.telegram_chat_id_get(a) == "111"


def test_accept_terms(db, two_users):
    a, b = two_users
    db.accept_terms(b)
    rows = {r["id"]: r for r in db._query("SELECT id, accepted_terms_at FROM users")}
    assert rows[a]["accepted_terms_at"] is None
    assert rows[b]["accepted_terms_at"] is not None


def test_turns(db, two_users):
    """A turn row belongs to the user who took the turn; recording B's turns
    never touches A's rows."""
    import uuid

    from tests.integration.test_db_functions import _turn_kwargs

    a, b = two_users
    ta, tb = uuid.uuid4().hex, uuid.uuid4().hex
    db.turn_record(**_turn_kwargs(ta, a))
    db.turn_record(**_turn_kwargs(tb, b, status="error"))
    assert db.turns_by_error_id(ta[:8])[0]["user_id"] == a
    assert db.turns_by_error_id(ta[:8])[0]["status"] == "ok"
    assert db.turns_by_error_id(tb[:8])[0]["user_id"] == b


COVERED = {
    "portfolio_add": test_portfolio,
    "portfolio_remove": test_portfolio,
    "portfolio_all": test_portfolio,
    "portfolio_update": test_portfolio_lots_are_per_user,
    "portfolio_remove_lot": test_portfolio_lots_are_per_user,
    "portfolio_import": test_portfolio_lots_are_per_user,
    "download_put": test_downloads_are_per_user,
    "download_get": test_downloads_are_per_user,
    "user_onboarding": test_onboarding_is_per_user,
    "user_mark_onboarded": test_onboarding_is_per_user,
    "watchlist_add": test_watchlist,
    "watchlist_remove": test_watchlist,
    "watchlist_all": test_watchlist,
    "session_create": test_sessions_and_chat_history,
    "session_list": test_sessions_and_chat_history,
    "session_touch": test_sessions_and_chat_history,
    "session_pending_get": test_session_pending,
    "session_pending_set": test_session_pending,
    "trial_tokens_used": test_trial_tokens,
    "trial_tokens_add": test_trial_tokens,
    "feedback_record": test_feedback,
    "feedback_for": test_feedback,
    "session_delete": test_sessions_and_chat_history,
    "chat_save": test_sessions_and_chat_history,
    "chat_load": test_sessions_and_chat_history,
    "report_save": test_reports,
    "report_list": test_reports,
    "report_get": test_reports,
    "screener_save": test_saved_screeners,
    "screener_list": test_saved_screeners,
    "alert_add": test_alerts,
    "alert_list": test_alerts,
    "alert_remove": test_alerts,
    "alert_pause": test_alerts,
    "user_is_admin": test_admin_console_scope,
    "user_set_disabled": test_admin_console_scope,
    "user_set_admin": test_admin_console_scope,
    "turns_recent": test_admin_console_scope,
    "session_rename": test_sessions_and_chat_history,
    "session_get_title": test_sessions_and_chat_history,
    "session_search": test_sessions_and_chat_history,
    "alert_get": test_alerts,
    "alert_update": test_alerts,
    "alert_resume": test_alerts,
    "notification_add": test_notifications,
    "notifications_list": test_notifications,
    "notifications_unread_count": test_notifications,
    "notifications_mark_read": test_notifications,
    "profile_get": test_financial_profile,
    "profile_upsert": test_financial_profile,
    "profile_clear": test_financial_profile,
    "llm_config_get": test_llm_config,
    "llm_config_set": test_llm_config,
    "llm_config_clear": test_llm_config,
    "llm_config_rewrap": test_llm_config,
    "auth_session_create": test_auth_sessions,
    "auth_sessions_list": test_auth_sessions,
    "auth_sessions_revoke_others": test_auth_sessions,
    "auth_token_create": test_account_lifecycle_functions,
    "user_email": test_account_lifecycle_functions,
    "user_password_hash": test_account_lifecycle_functions,
    "user_mark_verified": test_account_lifecycle_functions,
    "user_set_password": test_account_lifecycle_functions,
    "user_rehash_password": test_account_lifecycle_functions,
    "user_delete": test_account_lifecycle_functions,
    "user_rows": test_data_export_rows,
    "user_account_row": test_data_export_rows,
    "trial_quota_check_and_increment": test_trial_quota,
    "telegram_link_code_create": test_telegram_linking,
    "telegram_chat_id_set": test_telegram_linking,
    "telegram_chat_id_get": test_telegram_linking,
    "accept_terms": test_accept_terms,
    "turn_record": test_turns,
}


def test_every_user_scoped_db_function_is_covered():
    import utils.db as db_module

    user_scoped = {
        name
        for name, fn in inspect.getmembers(db_module, inspect.isfunction)
        if fn.__module__ == db_module.__name__
        and not name.startswith("_")  # private helpers are reached only through covered functions
        and "user_id" in inspect.signature(fn).parameters
    }
    missing = sorted(user_scoped - set(COVERED))
    assert missing == [], f"user-scoped db functions without an isolation test: {missing}"


# ── graph tools (through execute_task, with the real evidence cache) ──────────


def _run(tool, entity, user_id, query, intent="alert", extra=None):
    from graph.nodes.execute_task import execute_task

    task = {
        "id": f"{tool}:{entity}",
        "tool": tool,
        "entity": entity,
        "description": "",
        "depends_on": [],
        "status": "pending",
        "result_evidence_ids": [],
    }
    ctx = {
        "_user_id": user_id,
        "_query": query,
        "_llm_config": None,
        "_intent": intent,
        "_secondary_intents": [],
    }
    ctx.update(extra or {})
    return execute_task(task, ctx)["payload"]


def test_alert_tools_never_cross_users(db, two_users):
    a, b = two_users
    _run("alert_create", "TCS", a, "alert me if TCS drops below 3500")
    assert _run("alert_list", "market", b, "show my alerts") == []
    b_list = _run("alert_list", "market", b, "show my alerts")
    for tool, verb in (("alert_cancel", "cancel"), ("alert_pause", "pause"), ("alert_resume", "resume")):
        out = _run(tool, "TCS", b, f"{verb} my TCS alert", extra={"alert_list": b_list})
        assert out["result"] == "not_found", (tool, out)
    a_alerts = db.alert_list(a)
    assert len(a_alerts) == 1 and a_alerts[0]["active"] is True


def test_screener_save_writes_only_for_the_caller(db, two_users):
    a, b = two_users
    _run(
        "screener_save",
        "market",
        b,
        "save this screen as bvalue",
        intent="screener",
        extra={"screener_parse_query": {"pe_max": 20}},
    )
    assert db.screener_list(a) == []
    assert len(db.screener_list(b)) == 1


def test_two_users_creating_same_alert_within_a_minute_get_two_rows(db, two_users):
    a, b = two_users
    q = "alert me if TCS drops below 3500"
    first = _run("alert_create", "TCS", a, q)
    second = _run("alert_create", "TCS", b, q)
    assert first["result"] == second["result"] == "created"
    assert first["alert_id"] != second["alert_id"]
    assert len(db.alert_list(a)) == 1 and len(db.alert_list(b)) == 1


def test_same_user_creating_two_alerts_gets_two_rows(db, two_users):
    """DATA-01: a second create is never served from the cache — and since
    VAL-04 an identical one is reported as a duplicate rather than saved twice."""
    a, _ = two_users
    first = _run("alert_create", "TCS", a, "alert me if TCS drops below 3500")
    second = _run("alert_create", "TCS", a, "alert me if TCS drops below 3400")
    same_again = _run("alert_create", "TCS", a, "alert me if TCS drops below 3500")
    assert (first["result"], second["result"]) == ("created", "created")
    assert same_again["result"] == "duplicate" and same_again["alert_id"] == first["alert_id"]
    assert len(db.alert_list(a)) == 2


def test_top_losers_after_top_gainers_returns_losers(db, two_users):
    a, b = two_users
    fake = {"gainers": [{"symbol": "UP", "pct": 5}], "losers": [{"symbol": "DOWN", "pct": -5}]}
    with patch("graph.tool_catalogue.get_top_movers", side_effect=lambda d: fake[d]) as gtm:
        g = _run("fetch_top_movers", "market", a, "top gainers today", intent="screener")
        losers = _run("fetch_top_movers", "market", b, "top losers today", intent="screener")
        again = _run("fetch_top_movers", "market", a, "show top gainers", intent="screener")
    assert g == fake["gainers"] and losers == fake["losers"] and again == fake["gainers"]
    assert gtm.call_count == 2  # third call is a legitimate cache hit


def test_two_different_screens_return_different_results(db, two_users):
    a, b = two_users

    def parse(q):
        return {"sector": "IT"} if "it stocks" in q.lower() else {"sector": "Banking"}

    def run(filters):
        return [{"symbol": "TCS"}] if filters.get("sector") == "IT" else [{"symbol": "HDFCBANK"}]

    with (
        patch("graph.tool_catalogue.screener_parse_query", side_effect=parse),
        patch("graph.tool_catalogue.run_screener", side_effect=run),
    ):
        f1 = _run("screener_parse_query", "market", a, "IT stocks with PE below 30", intent="screener")
        r1 = _run("screener_run", "market", a, "", intent="screener", extra={"screener_parse_query": f1})
        f2 = _run("screener_parse_query", "market", b, "Banks with ROE above 15", intent="screener")
        r2 = _run("screener_run", "market", b, "", intent="screener", extra={"screener_parse_query": f2})
    assert r1 == [{"symbol": "TCS"}] and r2 == [{"symbol": "HDFCBANK"}]
