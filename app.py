"""
app.py — ARIA's entry point: process start-up and the Gradio page that ties the
tabs together (Gradio 6 compatible).

The tabs live in ui/ (chat, watchlist, portfolio, settings) over the business
logic in services/ (ARCH-02); this file only composes them. Run it with
`python app.py`, which starts the ASGI server in server.py.
"""

import logging
import os

import gradio as gr

from utils.logging_setup import configure_logging

# Before anything else logs: JSON lines with turn ids + secret redaction (OBS-01).
configure_logging()

from config import check_boot  # noqa: E402
from ui.admin import build_admin_tab  # noqa: E402
from ui.alerts import build_alerts_tab  # noqa: E402
from ui.auth import identify_user  # noqa: E402
from ui.chat import CHAT_DEFAULTS, build_chat_tab, queue_kwargs  # noqa: E402
from ui.portfolio import build_portfolio_tab  # noqa: E402
from ui.settings import build_settings_tab  # noqa: E402
from ui.theme import FOOTER_HTML, LOGOUT_JS, THEME_TOGGLE_JS, header_for, header_html  # noqa: E402
from ui.watchlist import build_watchlist_tab  # noqa: E402
from utils.db import ensure_schema  # noqa: E402
from utils.helpers import now_ist  # noqa: E402
from utils.tracing import warn_if_auto_tracing_on  # noqa: E402

log = logging.getLogger(__name__)


def startup() -> None:
    """Process start-up, run once before serving (not at import, so tests and
    tools can import this module without touching a database).

    ARCH-03: every deployment problem (DATABASE_URL, secrets, providers) is
    reported at once and the process exits non-zero. ARCH-01: the schema must
    already be at head (migrations are a release step)."""
    check_boot()
    ensure_schema()
    # OBS-02: refuse silently-unredacted tracing setups loudly.
    warn_if_auto_tracing_on()
    # REL-05: background jobs (alert check, housekeeping, Telegram poller,
    # nightly refreshes) run in worker.py; in dev, in this process too.
    from config import RUN_JOBS_IN_APP

    if RUN_JOBS_IN_APP:
        import threading

        import worker

        worker.start(threading.Event())
    # LLM-08: probe every routed model once, in the background.
    from agents.model_health import start_model_health_check

    start_model_health_check()


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="ARIA — Autonomous Financial Research Engine") as demo:
        # SEC-04: who is logged in comes from the session cookie, checked by
        # server.py's auth_dependency on every request (gr.Request.username).
        # user_state is set once per page load from that, never from input.
        user_state = gr.State(None)

        with gr.Column(elem_id="main-app"):
            header = gr.HTML(header_html())
            with gr.Row(elem_id="top-actions"):
                theme_btn = gr.Button("◐ Light / dark", size="sm", variant="secondary", elem_id="theme-btn")
                logout_btn = gr.Button("Log out", size="sm", variant="secondary", elem_id="logout-btn")
            theme_btn.click(None, js=THEME_TOGGLE_JS)
            logout_btn.click(None, js=LOGOUT_JS)

            # Tabs — one unified chat (orchestrator-routed) + the utility views
            with gr.Tabs():
                with gr.Tab("💬 Chat"):
                    chat_chatbot, chat_chart, chat_session, chat_sugg_state = build_chat_tab(user_state)
                with gr.Tab("📌 Watchlist"):
                    build_watchlist_tab(user_state)
                with gr.Tab("📊 Portfolio"):
                    build_portfolio_tab(user_state)
                with gr.Tab("🔔 Alerts") as alerts_tab:
                    build_alerts_tab(user_state, alerts_tab)
                with gr.Tab("⚙️ Settings"):
                    build_settings_tab(user_state)
                # OBS-05: shown to admins only; each handler checks on the server too
                with gr.Tab("🛠 Admin", visible=False) as admin_tab:
                    build_admin_tab(user_state, admin_tab)

            gr.HTML(FOOTER_HTML)

        # Reset the visible chat + active session on login/logout, so a
        # different account signing in on the same browser tab never sees the
        # previous user's conversation (UI-level tenant isolation, not just DB).
        def _reset_chat(user_id):
            return [], gr.update(value=None, visible=False), CHAT_DEFAULTS, None

        # show_progress="hidden" here and on every other event that writes the
        # chatbot: Gradio draws its own "•••" pending bubble on a target
        # component while such an event waits in the queue, and on a slow
        # first load it outlived the answer (TEST-07 found it on the container).
        user_state.change(
            _reset_chat,
            [user_state],
            [chat_chatbot, chat_chart, chat_sugg_state, chat_session],
            show_progress="hidden",
        )

        user_state.change(header_for, [user_state], [header], show_progress="hidden")

        demo.load(identify_user, None, [user_state], show_progress="hidden")

    demo.queue(**queue_kwargs())
    return demo


if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    print("=" * 62)
    print("  ⚡ ARIA v2 — Professional Investment Research Platform")
    print("  NSE · BSE · Mutual Funds · Screener")
    print("=" * 62)
    print(f"  Started  : {now_ist()}")
    from config import LOCAL_LLM_BASE_URL, LOCAL_LLM_MODEL, PORT

    print(f"  URL      : http://localhost:{PORT}")
    print("  Models   : per-node routing, see config.MODEL_ROUTING")
    if LOCAL_LLM_BASE_URL:
        print(f"  Local    : {LOCAL_LLM_MODEL} at {LOCAL_LLM_BASE_URL} (last resort)")
    print("=" * 62)
    startup()

    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,0.0.0.0")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1,0.0.0.0")

    # SEC-04: FastAPI (login/signup/logout, cookie sessions) around the
    # Gradio UI — see server.py.
    from server import serve

    serve()
