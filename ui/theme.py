"""
ui/theme.py — the look of the app: stylesheet, theme-toggle script, header and footer.

The stylesheet is ui/theme.css (colours are `--aria-*` tokens, AA contrast
checked by tests/test_a11y_tokens.py) plus the icon masks from ui/icons.py.
APP_CSS is their text. Dark is the default; a stored choice of light wins.
"""

import html
from pathlib import Path

import gradio as gr

from ui import icons

APP_CSS = (Path(__file__).with_name("theme.css")).read_text(encoding="utf-8") + icons.css()

# Fonts (Geist for the interface, Geist Mono for figures, Fraunces for a few display
# moments) and a pre-paint step: `dark` goes on <html> before the first frame unless
# the visitor chose light, so a dark page never flashes white. CSP allows both
# (web/security_headers.py: fonts.googleapis.com / fonts.gstatic.com, inline script).
HEAD_HTML = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap">
<script>(function () {
  var pick = null;
  try { pick = localStorage.getItem('aria-theme'); } catch (e) {}
  if (pick !== 'light') document.documentElement.classList.add('dark');
})();</script>
"""

# UX-03: dark unless the visitor chose light (stored), and the header toggle flips
# and remembers it. Gradio applies its own dark vars when `.dark` is on <body>, and
# APP_CSS's tokens follow the same class (also set on <html> by HEAD_HTML).
# Icon-only buttons get accessible names here because gr.Button has no aria-label;
# @gr.render re-creates the sidebar, so a MutationObserver labels new ones. Must be
# an IIFE — Gradio evaluates `js` as a bare expression.
APP_JS = """(() => {
  const KEY = 'aria-theme';
  const stored = () => { try { return localStorage.getItem(KEY); } catch (e) { return null; } };
  const apply = () => {
    const dark = stored() !== 'light';
    document.body.classList.toggle('dark', dark);
    document.documentElement.classList.toggle('dark', dark);
    // The login and account pages are rendered without script; they read this cookie (web/auth_routes.render).
    document.cookie = 'aria_theme=' + (dark ? 'dark' : 'light') + '; path=/; max-age=31536000; samesite=lax';
    const btn = document.getElementById('theme-btn');
    if (btn) {
      btn.setAttribute('aria-pressed', String(dark));
      btn.setAttribute('title', dark ? 'Switch to light theme' : 'Switch to dark theme');
    }
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
  // Gradio sets its own `.dark` on <body> from the OS on load; keep the visitor's choice.
  new MutationObserver(() => {
    if (document.body.classList.contains('dark') !== (stored() !== 'light')) apply();
  }).observe(document.body, {attributes: true, attributeFilter: ['class']});
  apply();
  label();
})()"""

THEME_TOGGLE_JS = "() => { window.ariaToggleTheme && window.ariaToggleTheme(); }"

_HEADER_TEMPLATE = """
<header id='aria-header'>
  <div class='aria-brand'>
    <span class='aria-wordmark' aria-label='ARIA'>AR<span>I</span>A</span>
    <span class='aria-tagline'>Research desk for Indian markets · NSE · BSE</span>
  </div>
  <div class='aria-pill'>{pill}</div>
</header>
"""


def header_html(pill: str = "Free trial · bring your own key for unlimited use (Settings)") -> str:
    """The page header; the pill says what limits the signed-in account."""
    return _HEADER_TEMPLATE.format(pill=html.escape(pill))


HEADER_HTML = header_html()


def header_for(user_id: int | None):
    """The header for whoever just signed in (an update, or no change while unknown)."""
    from services.settings import header_pill

    return header_html(header_pill(user_id)) if user_id else gr.update()


def build_app_bar() -> gr.HTML:
    """The sticky top bar: wordmark and plan pill, then the theme and log-out
    buttons. Returns the header so the caller can update the pill on sign-in."""
    with gr.Row(elem_id="app-bar"):
        header = gr.HTML(header_html())
        theme_btn = gr.Button("Switch theme", size="sm", elem_id="theme-btn")
        logout_btn = gr.Button(
            "Log out", size="sm", elem_id="logout-btn", elem_classes=["aria-ico", "ico-logout"]
        )
    theme_btn.click(None, js=THEME_TOGGLE_JS)
    logout_btn.click(None, js=LOGOUT_JS)
    return header


def page_header(title: str, lede: str) -> str:
    """Title and one line of context at the top of a utility tab. `lede` is trusted
    markup written in this repo (it may hold <strong>/<em>), never user text."""
    return f"<div class='page-head'><h2>{html.escape(title)}</h2><p>{lede}</p></div>"


def section_header(title: str, lede: str = "") -> str:
    """A titled section inside a tab (Settings), with an optional line of context."""
    line = f"<p>{lede}</p>" if lede else ""
    return f"<div class='section-head'><h3>{html.escape(title)}</h3>{line}</div>"


FOOTER_HTML = """
<div class='aria-footer'>
  Data: Yahoo Finance (via yfinance), NSE, BSE, AMFI, mfapi.in, Google News and publisher RSS
  <span class='sep'>·</span>
  Not SEBI-registered. Educational information, not investment advice.
</div>
"""

# SEC-04: logout is a POST to the FastAPI route (it deletes the session row and
# the HttpOnly cookie), then back to the login page.
LOGOUT_JS = (
    "() => { fetch('/auth/logout', {method: 'POST', credentials: 'same-origin'})"
    ".finally(() => { window.location.href = '/login'; }); }"
)
