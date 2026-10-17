"""
ui/theme.py — the look of the app: stylesheet, theme-toggle script, header and footer.

The stylesheet is ui/theme.css (colours are `--aria-*` tokens, AA contrast
checked by tests/test_a11y_tokens.py). APP_CSS is its text.
"""

from pathlib import Path

import gradio as gr

APP_CSS = (Path(__file__).with_name("theme.css")).read_text(encoding="utf-8")

# UX-03: light or dark follows the visitor's stored choice, else their OS
# preference (prefers-color-scheme), and the header toggle flips and remembers
# it. Gradio applies its own dark vars when `.dark` is on <body>, and APP_CSS's
# tokens follow the same class. Icon-only buttons get accessible names here
# because gr.Button has no aria-label; @gr.render re-creates the sidebar, so a
# MutationObserver labels new ones. Must be an IIFE — Gradio evaluates `js` as
# a bare expression.
APP_JS = """(() => {
  const KEY = 'aria-theme';
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const stored = () => { try { return localStorage.getItem(KEY); } catch (e) { return null; } };
  const apply = () => {
    const pick = stored();
    const dark = pick ? pick === 'dark' : mq.matches;
    document.body.classList.toggle('dark', dark);
    const btn = document.getElementById('theme-btn');
    if (btn) btn.setAttribute('aria-pressed', String(dark));
  };
  window.ariaToggleTheme = () => {
    const dark = !document.body.classList.contains('dark');
    try { localStorage.setItem(KEY, dark ? 'dark' : 'light'); } catch (e) {}
    apply();
  };
  const NAMES = {'◧': 'Show or hide chat history', '✎': 'New chat', '✕': 'Delete this chat'};
  let queued = false;
  let collapsedOnce = false;
  const label = () => {
    queued = false;
    document.querySelectorAll('button').forEach((b) => {
      const name = NAMES[b.textContent.trim()];
      if (name && b.getAttribute('aria-label') !== name) {
        b.setAttribute('aria-label', name);
        b.setAttribute('title', name);
      }
    });
    // Gradio keeps an aria-hidden copy of the tab strip (to measure overflow)
    // whose buttons can still take focus; `inert` takes them out of the tab order.
    document.querySelectorAll('.tab-container.visually-hidden[aria-hidden="true"]').forEach((el) => {
      el.inert = true;
    });
    const btn = document.getElementById('theme-btn');
    if (btn && !btn.hasAttribute('aria-pressed')) apply();
    // UX-08: on a phone the chat history starts collapsed (once per page load).
    if (!collapsedOnce && window.matchMedia('(max-width: 767px)').matches) {
      const side = document.querySelector('.chat-sidebar:not(.collapsed)');
      const toggle = side && side.querySelector('.sidebar-icon-btn');
      if (toggle) { collapsedOnce = true; toggle.click(); }
    }
  };
  new MutationObserver(() => {
    if (!queued) { queued = true; requestAnimationFrame(label); }
  }).observe(document.body, {childList: true, subtree: true});
  mq.addEventListener('change', apply);
  apply();
  label();
})()"""

THEME_TOGGLE_JS = "() => { window.ariaToggleTheme && window.ariaToggleTheme(); }"

_HEADER_TEMPLATE = """
<header id='aria-header'>
  <div>
    <span class='aria-brand'>⚡ ARIA</span>
    <span class='aria-tagline'>Autonomous Research &amp; Investment Analyst · NSE · BSE · v2</span>
  </div>
  <div class='aria-pill'>{pill}</div>
</header>
"""


def header_html(pill: str = "Free trial · bring your own key for unlimited use (Settings)") -> str:
    """The page header; the pill says what limits the signed-in account."""
    import html

    return _HEADER_TEMPLATE.format(pill=html.escape(pill))


HEADER_HTML = header_html()


def header_for(user_id: int | None):
    """The header for whoever just signed in (an update, or no change while unknown)."""
    from services.settings import header_pill

    return header_html(header_pill(user_id)) if user_id else gr.update()


FOOTER_HTML = """
<div class='aria-footer'>
  Data: Yahoo Finance (via yfinance), NSE, BSE, AMFI, mfapi.in, Google News and publisher RSS &nbsp;|&nbsp;
  ⚠️ Not SEBI-registered. Educational information, not investment advice.
</div>
"""

# SEC-04: logout is a POST to the FastAPI route (it deletes the session row and
# the HttpOnly cookie), then back to the login page.
LOGOUT_JS = (
    "() => { fetch('/auth/logout', {method: 'POST', credentials: 'same-origin'})"
    ".finally(() => { window.location.href = '/login'; }); }"
)
