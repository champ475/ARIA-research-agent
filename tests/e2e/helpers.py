"""Shared steps for the TEST-04 journeys."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.conftest import PASSWORD

ANSWER_TIMEOUT = 60_000


def signup(page: Page, base_url: str, email: str, agree: bool = True) -> None:
    page.goto(f"{base_url}/signup")
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    if agree:
        page.check("#agree")
    page.click("button[type=submit]")


def login(page: Page, base_url: str, email: str, password: str = PASSWORD) -> None:
    page.goto(f"{base_url}/login")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base_url}/**")


def new_user(page: Page, base_url: str, email: str) -> None:
    """Sign up, log in and wait for the chat to be ready."""
    signup(page, base_url, email)
    login(page, base_url, email)
    expect(page.locator("#msg-chat textarea")).to_be_visible(timeout=30_000)


def ask(page: Page, text: str) -> None:
    box = page.locator("#msg-chat textarea")
    expect(box).to_be_enabled(timeout=ANSWER_TIMEOUT)
    before = page.locator("#chatbot-chat [data-testid='bot']").count()
    box.fill(text)
    box.press("Enter")
    # The turn has begun once a new assistant bubble (the loading dots) shows.
    page.wait_for_function(
        "n => document.querySelectorAll('#chatbot-chat [data-testid=\"bot\"]').length > n",
        arg=before,
        timeout=ANSWER_TIMEOUT,
    )


def last_answer(page: Page):
    return page.locator("#chatbot-chat [data-testid='bot']").last


def wait_answer(page: Page, contains: str | None = None) -> str:
    """Wait until the turn is over — no loading dots or progress line, no
    pending bubble, input unlocked — and return the last bot message text."""
    page.wait_for_function(
        """() => {
            const chat = document.querySelector('#chatbot-chat');
            const box = document.querySelector('#msg-chat textarea');
            return chat && box && !box.disabled
                && !chat.querySelector('.aria-dot, .aria-progress, .pending')
                && chat.querySelectorAll('[data-testid="bot"]').length > 0;
        }""",
        timeout=ANSWER_TIMEOUT,
    )
    msg = last_answer(page)
    if contains:
        expect(msg).to_contain_text(contains, timeout=ANSWER_TIMEOUT)
    return msg.inner_text()


def open_tab(page: Page, name: str) -> None:
    page.get_by_role("tab", name=name).click()
