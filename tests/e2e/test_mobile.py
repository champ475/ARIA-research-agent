"""UX-08: at 375, 768 and 1280 px no page scrolls sideways, the message box is
usable, and on a phone the chat history starts collapsed above the chat.
Screenshots of each size are kept under test-results/mobile/ (CI uploads them)."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import ask, new_user, wait_answer

SIZES = [(375, 740), (768, 900), (1280, 800)]
TABS = ["Watchlist", "Portfolio", "Alerts", "Settings"]
OUT = Path(__file__).resolve().parents[2] / "test-results" / "mobile"


def _go_tab(page: Page, name: str) -> None:
    """Open a tab; on a narrow window Gradio folds the later tabs into a "..." menu."""
    tab = page.get_by_role("tab", name=name)
    if tab.count() and tab.first.is_visible():
        tab.first.click()
        return
    page.locator(".overflow-menu > button").click()
    page.locator(".overflow-dropdown button", has_text=name.split()[-1]).click()


def _overflow(page: Page) -> int:
    """How many pixels wider than the window the page is (0 = no sideways scroll)."""
    return page.evaluate(
        "() => Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - window.innerWidth"
    )


@pytest.mark.parametrize("width,height", SIZES)
def test_no_horizontal_scroll_and_a_usable_input(page: Page, base_url, email, width, height):
    page.set_viewport_size({"width": width, "height": height})
    OUT.mkdir(parents=True, exist_ok=True)
    new_user(page, base_url, email)

    box = page.locator("#msg-chat textarea")
    expect(box).to_be_visible()
    page.screenshot(path=str(OUT / f"chat-{width}.png"))
    box_rect = box.bounding_box()
    assert box_rect and box_rect["x"] >= 0 and box_rect["x"] + box_rect["width"] <= width + 1
    assert box_rect["width"] >= min(200, width * 0.6), f"input box too narrow: {box_rect}"
    assert _overflow(page) <= 1, "chat tab scrolls sideways"

    ask(page, "Analyse TCS")
    wait_answer(page, "Here is what the research shows")
    assert _overflow(page) <= 1, "chat tab scrolls sideways after an answer with a table and a chart"
    page.screenshot(path=str(OUT / f"chat-answer-{width}.png"))
    expect(box).to_be_enabled()
    box.fill("and the RSI?")
    expect(box).to_have_value("and the RSI?")

    for tab in TABS:
        _go_tab(page, tab)
        page.wait_for_timeout(400)
        assert _overflow(page) <= 1, f"{tab} scrolls sideways at {width}px"
        page.screenshot(path=str(OUT / f"{tab.split()[-1].lower()}-{width}.png"))


def test_history_drawer_starts_collapsed_on_a_phone_and_open_on_a_desktop(page: Page, base_url, email):
    page.set_viewport_size({"width": 375, "height": 740})
    new_user(page, base_url, email)
    sidebar = page.locator(".chat-sidebar")
    expect(sidebar).to_have_class(__import__("re").compile(r"\bcollapsed\b"), timeout=10_000)
    sidebar.locator(".sidebar-icon-btn").first.click()  # one tap opens it
    expect(sidebar).not_to_have_class(__import__("re").compile(r"\bcollapsed\b"))
    assert _overflow(page) <= 1

    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(page.url)
    page.wait_for_selector(".chat-sidebar")
    page.wait_for_timeout(800)
    assert "collapsed" not in (page.locator(".chat-sidebar").get_attribute("class") or "")
