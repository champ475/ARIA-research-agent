"""UX-03: the app's colour tokens meet WCAG 2.1 AA contrast in both themes,
text is never smaller than 12px, and no raw colour hides outside the tokens."""

from __future__ import annotations

import re

import pytest

from tests.conftest import import_app_without_side_effects

app = import_app_without_side_effects()

# (foreground, background) token pairs that carry text.
TEXT_PAIRS = [
    ("text", "bg"),
    ("text", "surface"),
    ("text-2", "surface"),
    ("text-2", "sunken"),
    ("muted", "bg"),
    ("muted", "surface"),
    ("muted", "sunken"),
    ("accent-fg", "bg"),
    ("accent-fg", "surface"),
    ("accent-fg", "surface-2"),
    ("accent-fg", "accent-soft"),
    ("on-accent", "accent-strong"),
    ("on-accent", "accent-hover"),
    ("danger", "bg"),
    ("code", "surface-2"),
]


def _tokens(selector: str) -> dict[str, str]:
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", app.APP_CSS, flags=re.S)
    assert block, selector
    return dict(re.findall(r"--aria-([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block.group(1)))


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_formula():
    assert round(contrast("#000000", "#ffffff"), 1) == 21.0
    assert round(contrast("#767676", "#ffffff"), 2) == 4.54


@pytest.mark.parametrize("theme", [":root", ".dark"])
@pytest.mark.parametrize("fg, bg", TEXT_PAIRS)
def test_text_pairs_meet_aa(theme, fg, bg):
    tokens = _tokens(theme)
    assert contrast(tokens[fg], tokens[bg]) >= 4.5, (theme, fg, bg)


def test_both_themes_define_the_same_tokens():
    assert _tokens(":root").keys() == _tokens(".dark").keys()


def test_no_text_below_12px():
    sizes = [
        int(n) for n in re.findall(r"font-size:\s*(\d+)px", app.APP_CSS + app.HEADER_HTML + app.FOOTER_HTML)
    ]
    assert sizes and min(sizes) >= 12


def test_colours_only_in_token_blocks():
    outside = re.sub(r"(:root|\.dark)\s*\{.*?\}", "", app.APP_CSS, count=2, flags=re.S)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", outside + app.HEADER_HTML + app.FOOTER_HTML)


def test_icon_buttons_get_names():
    for glyph, name in (("◧", "Show or hide chat history"), ("✎", "New chat"), ("✕", "Delete this chat")):
        assert glyph in app.APP_JS and name in app.APP_JS
