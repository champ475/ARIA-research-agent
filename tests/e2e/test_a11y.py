"""UX-03: axe-core finds no serious or critical WCAG 2.1 AA violation on the
auth gate or any app tab, in the light and the dark theme."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import new_user, open_tab

AXE = Path(__file__).with_name("vendor") / "axe.min.js"
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]


def _violations(page: Page) -> list[str]:
    # Scan a settled page: Gradio fades components while an update is pending.
    page.wait_for_function(
        "() => !document.querySelector('.gradio-container .pending, .gradio-container .generating')"
    )
    page.wait_for_timeout(400)
    if not page.evaluate("typeof axe !== 'undefined'"):
        page.add_script_tag(path=str(AXE))
    found = page.evaluate(
        """async tags => {
            const r = await axe.run(document, {runOnly: {type: 'tag', values: tags}, resultTypes: ['violations']});
            return r.violations
                .filter(v => v.impact === 'serious' || v.impact === 'critical')
                .map(v => `${v.id} (${v.impact}): ` + v.nodes.slice(0, 4).map(
                    n => `${n.target.join(' ')} ${n.html.slice(0, 160)} -- ${(n.any[0] || n.all[0] || {}).message || ''}`
                ).join(' || '));
        }""",
        TAGS,
    )
    return list(found)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_auth_gate(page: Page, base_url, scheme):
    page.emulate_media(color_scheme=scheme)
    for path in ("/login", "/signup"):
        page.goto(base_url + path)
        assert _violations(page) == [], path


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_app_tabs(page: Page, base_url, email, scheme):
    page.emulate_media(color_scheme=scheme)
    new_user(page, base_url, email)
    page.wait_for_function("() => window.ariaToggleTheme !== undefined")
    assert page.evaluate("document.body.classList.contains('dark')") == (scheme == "dark")
    problems = {"Chat": _violations(page)}
    for tab in ("📌 Watchlist", "📊 Portfolio", "🔔 Alerts", "⚙️ Settings"):
        open_tab(page, tab)
        problems[tab] = _violations(page)
    assert all(not v for v in problems.values()), problems


def test_theme_toggle_is_remembered_and_icons_are_labelled(page: Page, base_url, email):
    page.emulate_media(color_scheme="dark")
    new_user(page, base_url, email)
    toggle = page.locator("#theme-btn")
    expect(toggle).to_have_attribute("aria-pressed", "true")
    toggle.click()
    assert not page.evaluate("document.body.classList.contains('dark')")
    page.reload()
    expect(page.locator("#msg-chat textarea")).to_be_visible(timeout=30_000)
    expect(page.locator("#theme-btn")).to_have_attribute("aria-pressed", "false")
    assert page.get_by_role("button", name="Show or hide chat history").count() == 1
