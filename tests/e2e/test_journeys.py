"""TEST-04: the 15 user journeys, end to end in a real browser against a real
server (fake LLM, offline data, throwaway database)."""

from __future__ import annotations

import re
from datetime import datetime

from playwright.sync_api import Page, expect

from tests.e2e.conftest import PASSWORD
from tests.e2e.helpers import ANSWER_TIMEOUT, ask, login, new_user, open_tab, signup, wait_answer

ERROR = "Something went wrong"


def _user_id(db, email: str) -> int:
    return db.execute("SELECT id FROM users WHERE email=%s", (email,)).fetchone()["id"]


# 1 ─────────────────────────────────────────────────────────────────────────────
def test_signup_needs_consent_then_logs_in(page: Page, base_url, email, db):
    signup(page, base_url, email, agree=False)
    expect(page.locator("body")).to_contain_text("agree", ignore_case=True)
    assert db.execute("SELECT count(*) AS n FROM users WHERE email=%s", (email,)).fetchone()["n"] == 0
    signup(page, base_url, email)
    expect(page.locator("body")).to_contain_text("email", ignore_case=True)  # "check your email" page
    login(page, base_url, email)
    expect(page.locator("#msg-chat textarea")).to_be_visible(timeout=30_000)
    expect(page.locator("#onboarding-card")).to_contain_text("Welcome to ARIA")
    row = db.execute("SELECT accepted_terms_version FROM users WHERE email=%s", (email,)).fetchone()
    assert row["accepted_terms_version"]


# 2 ─────────────────────────────────────────────────────────────────────────────
def test_login_refresh_and_logout(page: Page, base_url, email):
    new_user(page, base_url, email)
    page.reload()
    expect(page.locator("#msg-chat textarea")).to_be_visible(timeout=30_000)  # still logged in
    page.locator("#logout-btn").click()
    page.wait_for_url(re.compile(r".*/login.*"), timeout=15_000)
    page.goto(base_url + "/")
    page.wait_for_url(re.compile(r".*/login.*"), timeout=15_000)
    page.fill("#email", email)
    page.fill("#password", "wrong-password-1")
    page.click("button[type=submit]")
    expect(page.locator("body")).to_contain_text("Invalid email or password")
    login(page, base_url, email, PASSWORD)
    expect(page.locator("#msg-chat textarea")).to_be_visible(timeout=30_000)


# 3 + 5 ─────────────────────────────────────────────────────────────────────────
def test_equity_question_streams_progress_answer_and_chart(page: Page, base_url, email):
    new_user(page, base_url, email)
    # A MutationObserver records every progress text as it is rendered, so a
    # fast turn on a slow runner cannot slip between two polls.
    page.evaluate(
        """() => {
            window.__aria_seen = new Set();
            const grab = () => document.querySelectorAll('#chatbot-chat .aria-progress')
                .forEach(e => window.__aria_seen.add(e.innerText));
            new MutationObserver(grab).observe(document.body, {subtree: true, childList: true, characterData: true});
        }"""
    )
    ask(page, "Analyse TCS")
    expect(page.locator("#msg-chat textarea")).to_be_disabled(timeout=10_000)  # the turn has started
    deadline = datetime.now().timestamp() + ANSWER_TIMEOUT / 1000
    while datetime.now().timestamp() < deadline and not page.locator("#msg-chat textarea").is_enabled():
        page.wait_for_timeout(150)
    seen = set(page.evaluate("() => [...window.__aria_seen]"))
    assert len(seen) >= 2, f"expected at least two progress states, saw {seen}"
    text = wait_answer(page, "Here is what the research shows")
    assert ERROR not in text and "Data as of" in text and "Routed to" not in text
    expect(page.locator("#chart-chat")).to_be_visible(timeout=10_000)
    assert "Chart in words." in text  # UX-03: the chart's text alternative
    # UX-07: a collapsed Sources block with source, as-of time and confidence
    sources = page.locator("#chatbot-chat details").last
    expect(sources).to_be_visible(timeout=10_000)
    assert sources.get_attribute("open") is None
    sources.locator("summary").click()
    expect(sources).to_contain_text("Yahoo Finance")
    expect(sources).to_contain_text("IST")
    expect(sources).to_contain_text("full")


# 4 ─────────────────────────────────────────────────────────────────────────────
def test_follow_up_uses_the_conversation(page: Page, base_url, email):
    new_user(page, base_url, email)
    ask(page, "Analyse INFY")
    wait_answer(page, "research shows")
    ask(page, "and its technicals?")
    text = wait_answer(page)
    assert ERROR not in text and "INFY" in text


# 6 ─────────────────────────────────────────────────────────────────────────────
def test_report_download(page: Page, base_url, email):
    new_user(page, base_url, email)
    ask(page, "IC report for TCS")
    wait_answer(page)
    md_link = page.get_by_role("link", name="⬇ Download Markdown").last
    expect(md_link).to_be_visible(timeout=ANSWER_TIMEOUT)
    md = page.request.get(base_url + md_link.get_attribute("href"))
    assert md.status == 200 and b"TCS" in md.body()
    pdf = page.request.get(
        base_url + page.get_by_role("link", name="⬇ Download PDF").last.get_attribute("href")
    )
    # The PDF engine (WeasyPrint) needs native libraries the CI image has; a dev box may not.
    assert (pdf.status == 200 and pdf.body().startswith(b"%PDF")) or pdf.status == 503


# 7 ─────────────────────────────────────────────────────────────────────────────
def test_mutual_fund_question(page: Page, base_url, email):
    new_user(page, base_url, email)
    ask(page, "Best ELSS for tax saving")
    text = wait_answer(page)
    assert ERROR not in text and "research shows" in text


# 8 ─────────────────────────────────────────────────────────────────────────────
def test_screener_and_csv(page: Page, base_url, email):
    new_user(page, base_url, email)
    ask(page, "IT stocks with PE below 40")
    text = wait_answer(page)
    assert ERROR not in text
    csv = page.get_by_role("link", name="⬇ Export results (CSV)").last
    expect(csv).to_be_visible(timeout=ANSWER_TIMEOUT)
    body = page.request.get(base_url + csv.get_attribute("href")).body()
    assert body.startswith(b"symbol,")


# 9 ─────────────────────────────────────────────────────────────────────────────
def test_create_list_and_cancel_an_alert(page: Page, base_url, email, db):
    new_user(page, base_url, email)
    ask(page, "Alert me if TCS goes above 99999 anyway")
    assert ERROR not in wait_answer(page)
    uid = _user_id(db, email)
    rows = db.execute("SELECT id, symbol, threshold FROM alerts WHERE user_id=%s", (uid,)).fetchall()
    assert [(r["symbol"], r["threshold"]) for r in rows] == [("TCS", 99999.0)]
    ask(page, "show my alerts")
    assert "TCS" in wait_answer(page)
    ask(page, "cancel my TCS alert")
    assert ERROR not in wait_answer(page)
    assert db.execute("SELECT count(*) AS n FROM alerts WHERE user_id=%s", (uid,)).fetchone()["n"] == 0


# 9b ────────────────────────────────────────────────────────────────────────────
def test_alerts_tab_create_in_chat_then_edit_and_delete(page: Page, base_url, email, db):
    """UX-06: an alert made in chat shows in the Alerts tab, can be paused, edited and deleted."""
    new_user(page, base_url, email)
    ask(page, "Alert me if TCS goes above 99999 anyway")
    assert ERROR not in wait_answer(page)
    uid = _user_id(db, email)
    aid = db.execute("SELECT id FROM alerts WHERE user_id=%s", (uid,)).fetchone()["id"]

    open_tab(page, "Alerts")
    table = page.locator("#alerts-table")
    expect(table).to_contain_text("TCS", timeout=ANSWER_TIMEOUT)
    expect(table).to_contain_text("Active")

    page.locator("#alert-id input").fill(str(aid))
    page.locator("#alert-pause").click()
    expect(page.locator("#alerts-status")).to_contain_text("paused", timeout=ANSWER_TIMEOUT)
    expect(table).to_contain_text("Paused", timeout=ANSWER_TIMEOUT)
    page.locator("#alert-resume").click()
    expect(table).to_contain_text("Active", timeout=ANSWER_TIMEOUT)

    page.get_by_text("Change the number").click()
    page.locator("#alert-value input, #alert-value textarea").first.fill("88888")
    page.locator("#alert-save").click()
    expect(page.locator("#alerts-status")).to_contain_text("Saved", timeout=ANSWER_TIMEOUT)
    assert db.execute("SELECT threshold FROM alerts WHERE id=%s", (aid,)).fetchone()["threshold"] == 88888

    page.locator("#alert-value input, #alert-value textarea").first.fill("-5")
    page.locator("#alert-save").click()
    expect(page.locator("#alerts-status")).to_contain_text("isn't allowed", timeout=ANSWER_TIMEOUT)
    assert db.execute("SELECT threshold FROM alerts WHERE id=%s", (aid,)).fetchone()["threshold"] == 88888

    page.locator("#alert-delete").click()
    expect(page.locator("#alerts-status")).to_contain_text("deleted", timeout=ANSWER_TIMEOUT)
    assert db.execute("SELECT count(*) AS n FROM alerts WHERE user_id=%s", (uid,)).fetchone()["n"] == 0


# 9c ────────────────────────────────────────────────────────────────────────────
def test_chat_history_rename_search_and_export(page: Page, base_url, email):
    """UX-09: rename the open chat, find it by what was said, export it as Markdown."""
    new_user(page, base_url, email)
    ask(page, "Analyse TCS")
    wait_answer(page, "Here is what the research shows")
    sidebar = page.locator(".chat-sidebar")
    expect(sidebar.locator(".session-btn")).to_have_count(1, timeout=10_000)

    page.get_by_text("This chat", exact=True).click()
    page.locator("#chat-rename input, #chat-rename textarea").first.fill("TCS deep dive")
    page.locator("#chat-rename-btn").click()
    expect(page.locator("#chat-manage-note")).to_contain_text("Renamed", timeout=10_000)
    expect(sidebar.locator(".session-btn").first).to_have_text("TCS deep dive", timeout=10_000)

    search = page.locator("#chat-search input, #chat-search textarea").first
    search.fill("nothingmatchesthis")
    search.press("Enter")
    expect(sidebar).to_contain_text("No chats match that", timeout=10_000)
    search.fill("tcs")  # in the chat's text and its new title
    search.press("Enter")
    expect(sidebar.locator(".session-btn").first).to_have_text("TCS deep dive", timeout=10_000)

    page.locator("#chat-export-btn").click()
    link = page.get_by_role("link", name="⬇ Download this chat (Markdown)")
    expect(link).to_be_visible(timeout=10_000)
    body = page.request.get(base_url + link.get_attribute("href")).text()
    assert body.startswith("# TCS deep dive") and "**You**" in body and "Analyse TCS" in body


# 9d ────────────────────────────────────────────────────────────────────────────
def test_admin_tab_is_for_admins_only(page: Page, base_url, email, db):
    """OBS-05: a normal user has no Admin tab; an admin finds a user and sees the facts."""
    new_user(page, base_url, email)
    expect(page.get_by_role("tab", name="Chat")).to_be_visible()
    assert page.get_by_role("tab", name="Admin").count() == 0

    db.execute("UPDATE users SET is_admin=TRUE WHERE email=%s", (email,))
    page.reload()
    expect(page.get_by_role("tab", name="Admin")).to_be_visible(timeout=15_000)
    open_tab(page, "Admin")
    page.locator("#admin-email input, #admin-email textarea").first.fill(email)
    page.locator("#admin-find").click()
    expect(page.locator("#admin-card")).to_contain_text(email, timeout=10_000)
    expect(page.locator("#admin-card")).to_contain_text("free messages today")
    page.locator("#admin-turns").click()
    expect(page.locator("#admin-detail")).to_be_visible()
    expect(page.locator("#admin-flags")).to_contain_text("signups_open", timeout=10_000)
    page.locator("#admin-disable").click()
    expect(page.locator("#admin-detail")).to_contain_text("can't disable your own account", timeout=10_000)


# 10 ────────────────────────────────────────────────────────────────────────────
def test_planning_intake_to_strategy(page: Page, base_url, email, db):
    new_user(page, base_url, email)
    ask(page, "I'm 30 and want to invest 10 lakh for retirement in 20 years with moderate risk")
    assert ERROR not in wait_answer(page)
    profile = db.execute(
        "SELECT profile FROM financial_profiles WHERE user_id=%s", (_user_id(db, email),)
    ).fetchone()["profile"]
    assert profile["age"] == 30 and profile["risk_tolerance"] == "moderate"
    ask(page, "build my plan")
    text = wait_answer(page)
    assert ERROR not in text and len(text) > 80


# 11 ────────────────────────────────────────────────────────────────────────────
def test_telegram_connect_link(page: Page, base_url, email):
    new_user(page, base_url, email)
    open_tab(page, "Settings")
    page.get_by_role("button", name="Connect Telegram").click()
    link = page.get_by_role("link", name="Tap here to connect")
    expect(link).to_have_attribute(
        "href", re.compile(r"^https://t\.me/aria_e2e_bot\?start=[\w-]+$"), timeout=15_000
    )


# 12 ────────────────────────────────────────────────────────────────────────────
def test_watchlist_add_and_remove(page: Page, base_url, email):
    new_user(page, base_url, email)
    open_tab(page, "Watchlist")
    page.get_by_placeholder("Add symbol (e.g. TCS, RELIANCE)").fill("Infosys")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator("#wl-table")).to_contain_text("INFY", timeout=30_000)
    page.get_by_placeholder("Add symbol (e.g. TCS, RELIANCE)").fill("<script>")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator("body")).to_contain_text(
        re.compile(r"Couldn't find|enter the exact NSE symbol"), timeout=15_000
    )
    expect(page.locator("#wl-table")).not_to_contain_text("script")  # rejected, not stored
    page.get_by_placeholder("Remove symbol").fill("INFY")
    page.get_by_role("button", name="Remove", exact=True).click()
    expect(page.locator("#wl-table")).not_to_contain_text("INFY", timeout=30_000)


# 13 ────────────────────────────────────────────────────────────────────────────
def test_byo_key_save_and_use(page: Page, base_url, email, db):
    new_user(page, base_url, email)
    open_tab(page, "Settings")
    page.get_by_label("Model").fill("gpt-4o-mini")
    page.get_by_label("API Key").fill("sk-e2e-test-key")
    page.get_by_role("button", name="Save key").click()
    expect(page.locator("body")).to_contain_text("Using your own key", timeout=15_000)
    row = db.execute(
        "SELECT model, api_key_encrypted FROM user_llm_config WHERE user_id=%s", (_user_id(db, email),)
    ).fetchone()
    assert row["model"] == "gpt-4o-mini" and "sk-e2e" not in row["api_key_encrypted"]
    open_tab(page, "Chat")
    ask(page, "Analyse TCS")
    assert ERROR not in wait_answer(page)


# 14 ────────────────────────────────────────────────────────────────────────────
def test_trial_quota_exhausted(page: Page, base_url, email, db):
    new_user(page, base_url, email)
    db.execute(
        "UPDATE users SET free_msgs_used_today = 5, free_msgs_reset_at = (now() AT TIME ZONE 'Asia/Kolkata')::date "
        "WHERE email=%s",
        (email,),
    )
    ask(page, "Analyse TCS")
    text = wait_answer(page)
    assert "free" in text.lower() and "Settings" in text


# 15 ────────────────────────────────────────────────────────────────────────────
def test_two_tabs_keep_separate_conversations(page: Page, base_url, email):
    new_user(page, base_url, email)
    other = page.context.new_page()
    other.goto(base_url + "/")
    expect(other.locator("#msg-chat textarea")).to_be_visible(timeout=30_000)
    ask(page, "Analyse TCS")
    ask(other, "Analyse WIPRO")
    wait_answer(page, "research shows")
    wait_answer(other, "research shows")
    a, b = page.locator("#chatbot-chat").inner_text(), other.locator("#chatbot-chat").inner_text()
    assert "TCS" in a and "WIPRO" not in a
    assert "WIPRO" in b and "Analyse TCS" not in b


def test_the_box_is_empty_and_has_the_cursor_after_an_answer(page: Page, base_url, email):
    """Live run 2026-10-18: the text stayed in the box and the next message went out glued to it."""
    new_user(page, base_url, email)
    ask(page, "Analyse TCS")
    wait_answer(page, "Here is what the research shows")
    box = page.locator("#msg-chat textarea")
    expect(box).to_have_value("")
    expect(box).to_be_focused()
    page.keyboard.type("and the RSI?")  # no click needed
    expect(box).to_have_value("and the RSI?")
