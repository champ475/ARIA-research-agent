"""
app.py — Gradio front end for ARIA, an autonomous financial research engine.
Gradio 6 compatible. show_api=False fixes the proxy/launch error.
"""

import logging
import os
import re
import threading
import time

import gradio as gr
from cachetools import TTLCache

from utils.logging_setup import configure_logging, run_in_turn

# Before anything else logs: JSON lines with turn ids + secret redaction (OBS-01).
configure_logging()

from config import (
    CHAT_CONCURRENCY,
    MAX_CHAT_CHARS,
    QUEUE_MAX_SIZE,
    TRIAL_DAILY_LIMIT,
    TURNS_PER_MINUTE,
    check_boot,
)
from graph.progress import ProgressUpdate
from utils import lifecycle
from utils.db import ensure_schema
from utils.errors import AriaError, ConfigurationError, UserInputError, error_id_from_turn, render_error
from utils.escaping import html_text, md_cell
from utils.helpers import IST as IST_TZ
from utils.helpers import now_ist
from utils.input_guard import CleanMessage, InputRejected, clean_message
from utils.tracing import warn_if_auto_tracing_on
from utils.url_safety import UnsafeURLError

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


# ── Loading indicator HTML (three bouncing dots, styled via global CSS) ──────
LOADING_HTML = '<span class="aria-dot"></span><span class="aria-dot"></span><span class="aria-dot"></span>'

# ── Lazy orchestrator loader ──────────────────────────────────────────────────
# One GraphOrchestrator per user (agents/graph_orchestrator.py) — the LangGraph
# research-graph engine, see RFC_AGENTIC_RESEARCH_GRAPH.md, is the only
# orchestration path now (the legacy keyword-routed Orchestrator + its 6
# specialist agent classes were retired once the graph reached feature parity
# across all 6 domains — see ARCHITECTURE.md).
#
# REL-03: a bounded cache (at most ORCHESTRATOR_CACHE_SIZE, evicted after
# ORCHESTRATOR_IDLE_SECONDS idle). An evicted conversation loses nothing: the
# next turn rebuilds its memory from chat_history and any pending
# confirmation from the session row. VAL-10: keyed by (user, chat session),
# so two tabs on two chats never share memory or a pending yes/no.
ORCHESTRATOR_CACHE_SIZE = 500
ORCHESTRATOR_IDLE_SECONDS = 1800
_orchestrators: TTLCache = TTLCache(maxsize=ORCHESTRATOR_CACHE_SIZE, ttl=ORCHESTRATOR_IDLE_SECONDS)
_orchestrators_lock = threading.Lock()


def get_orchestrator(user_id: int, session_id: int | None = None):
    # Concurrent turns (REL-01) mean two threads can race to build the same
    # conversation's orchestrator; the lock keeps it to exactly one instance.
    key = (user_id, session_id)
    with _orchestrators_lock:
        orch = _orchestrators.get(key)
        if orch is None:
            from agents.graph_orchestrator import GraphOrchestrator

            orch = GraphOrchestrator(user_id, session_id)
            _orchestrators[key] = orch
        else:
            _orchestrators[key] = orch  # refresh the idle timer
        return orch


def evict_user(user_id: int) -> None:
    """Drop every cached conversation of a user (e.g. after a BYO key change,
    so the next turn picks up the new provider)."""
    with _orchestrators_lock:
        for key in [k for k in list(_orchestrators.keys()) if k[0] == user_id]:
            _orchestrators.pop(key, None)


# ── Concurrency (REL-01) ──────────────────────────────────────────────────────
# Gradio defaults every event to concurrency_limit=1, so one user's 30-second
# report used to queue every other user's message behind it. Chat turns now
# share a pool of CHAT_CONCURRENCY workers (concurrency_id="chat"); each user
# still gets at most ONE turn in flight, because their GraphOrchestrator (and
# its conversation memory) is per-user state that isn't safe to drive from two
# threads at once — a second tab's message gets a "still working" reply.
WATCHLIST_CONCURRENCY = 4

# One turn in flight per conversation (VAL-10); bounded like the cache above.
_user_turn_locks: TTLCache = TTLCache(maxsize=ORCHESTRATOR_CACHE_SIZE * 2, ttl=ORCHESTRATOR_IDLE_SECONDS)
_user_turn_locks_guard = threading.Lock()


def _user_turn_lock(user_id, session_id=None) -> threading.Lock:
    with _user_turn_locks_guard:
        key = (user_id, session_id)
        lock = _user_turn_locks.get(key)
        if lock is None:
            lock = threading.Lock()
        _user_turn_locks[key] = lock
        return lock


def _queue_kwargs() -> dict:
    return {"default_concurrency_limit": CHAT_CONCURRENCY, "max_size": QUEUE_MAX_SIZE}


# ── Timeframe keyword map (appended to message when non-default, equity only) ─
_TF_KEYWORDS = {
    "1D": "",
    "1W": "weekly",
    "1M": "monthly",
    "3M": "3mo",
    "6M": "6mo",
    "2Y": "2y",
}

_TAB_LABELS = {
    "equity": "📊 Equity Research",
    "report": "📋 Report Generator",
    "mf": "📈 Mutual Funds",
    "screener": "🔍 Screener",
    "alert": "🔔 Alerts",
    "planning": "🧭 Financial Planner",
}


def _routing_badge(tab_used: str | None) -> str:
    """UX-02: the "Routed to" line is a debugging aid, off unless SHOW_ROUTING_BADGE."""
    from config import SHOW_ROUTING_BADGE

    return f"*🔀 Routed to: {_tab_label(tab_used or 'equity')}*\n\n" if SHOW_ROUTING_BADGE else ""


def _progress_html(steps: list[str]) -> str:
    """Loading dots plus the last few research steps (UX-02)."""
    shown = steps[-4:]
    trail = " → ".join(html_text(s) for s in shown[:-1])
    current = html_text(shown[-1]) + "…" if shown else ""
    return (
        f"{LOADING_HTML}<div class='aria-progress' role='status' aria-live='polite'>"
        f"{trail + ' → ' if trail else ''}<strong>{current}</strong></div>"
    )


def _tab_label(tab_used: str) -> str:
    """Renders a routing badge label for `tab_used`, including a joined
    multi-domain label (e.g. "report+alert" -> "📋 Report Generator + 🔔
    Alerts") when the graph orchestrator names more than one domain for this
    turn (Phase 7.4 multi-intent support) — falls back to the raw segment
    for any domain name not in _TAB_LABELS."""
    parts = tab_used.split("+")
    return " + ".join(_TAB_LABELS.get(p, p) for p in parts)


# UX-01: one example per domain a new user might not know about.
CHAT_DEFAULTS = [
    "Analyse TCS",
    "Alert me if INFY falls 5%",
    "Best ELSS for tax saving",
    "Plan ₹50L for retirement in 20 years",
]

# UX-01: always one click away in the chat tab.
CAPABILITIES_MD = """
**ARIA can**
- research any NSE/BSE-listed company: fundamentals (bank-aware), technicals, news and sentiment,
  peers, a risk profile, backtests and full research reports (PDF/Markdown);
- screen the whole NSE market ("IT stocks with PE below 25 and ROE above 18%");
- rank and compare mutual funds by returns, risk and cost;
- watch prices, % moves and indicators for you and alert you on Telegram or in the app;
- track your portfolio's P&L and build a goal-based plan from your profile.

**ARIA can't**
- tell you what to buy or sell: it's educational research, not SEBI-registered advice;
- see live tick data: prices come from public sources and can be delayed (each answer says when);
- cover markets outside India, derivatives strategies, crypto, or tax filing;
- be right every time: numbers come with their sources — check what matters before acting.
"""
CHAT_PLACEHOLDER = (
    "Ask anything — stock analysis, IC/result-update reports, mutual funds, "
    "screening, portfolio, market overview…"
)

WATCHLIST_COLS = ["Symbol", "Name", "Price", "Change %", "RSI", "52W High", "52W Low"]


def _session_title(msg: str) -> str:
    """First-message-derived title for a new chat session, ChatGPT-style."""
    t = " ".join(msg.strip().split())
    if not t:
        return "New chat"
    return (t[:42] + "…") if len(t) > 42 else t


# ── Core respond function (orchestrator-routed) ───────────────────────────────

_INPUT_BUSY = gr.update(value="", interactive=False)  # locked while a turn is in flight
_INPUT_READY = gr.update(value="", interactive=True)  # unlocked once the turn is done
_SEND_BUSY = gr.update(interactive=False)
_SEND_READY = gr.update(interactive=True)


def respond(user_id: int, message: str, history: list, suggestions: list, session_id: int, tf: str):
    """
    Generator: first yield shows loading dots, second yield delivers the real
    response. The orchestrator decides which specialist agent handles the
    message; the reply is prefixed with a small badge naming that agent so
    routing stays visible instead of being a hidden implementation detail.

    The input box and Send button are locked (interactive=False) for every
    yield except the terminal one — a rapid Enter/click issued while a turn
    is still in flight used to be silently swallowed by Gradio's client
    (confirmed via a Playwright audit: the resubmitted message never even
    reached the server), with nothing telling the user their message went
    nowhere. Locking the input for the round-trip closes that window instead
    of just detecting it after the fact.

    Yields: (cleared_input, send_btn, updated_history, chart, suggestions, *btn_updates)
    """
    # VAL-01: one input contract — normalise, strip invisible/control chars,
    # bound the length — and use the cleaned text everywhere below.
    checked = clean_message(message)
    if isinstance(checked, InputRejected):
        if checked.reason == "empty":
            yield (
                _INPUT_READY,
                _SEND_READY,
                history,
                gr.update(),
                suggestions,
            ) + tuple(gr.update() for _ in range(4))
            return
        log.info(f"[respond] input rejected: {checked.reason}")
        preview = message[:200] + "…" if isinstance(message, str) and len(message) > 200 else message
        rejected_hist = history + [
            {"role": "user", "content": preview or ""},
            {"role": "assistant", "content": f"⚠️ {checked.user_message}"},
        ]
        # Keep the user's text in the box so they can shorten it instead of retyping.
        keep_text = gr.update(value=message if isinstance(message, str) else "", interactive=True)
        yield (
            keep_text,
            _SEND_READY,
            rejected_hist,
            gr.update(),
            suggestions,
        ) + tuple(gr.update() for _ in range(4))
        return
    message = checked.text

    clean = message.lstrip("📊📋📈📰🔀🌍❓✅🔍 ").strip()
    started = time.monotonic()
    log.info(f"[respond] turn start chars={len(clean)}")

    # Timeframe keyword only makes sense when equity handles the message —
    # decide the route up front so it isn't injected into an unrelated agent.
    # Sourced from the SAME orchestrator instance that will actually process
    # this message (Orchestrator.classify()/GraphOrchestrator.classify(), both
    # single-purpose cheap pre-checks) so this decision and the real routing
    # decision can never disagree about which orchestrator is active. Any
    # failure here (e.g. missing API key) is swallowed — the actual
    # process_stream() call below re-attempts orchestrator construction and
    # surfaces the same failure as a proper "API Key Required" message.
    try:
        if get_orchestrator(user_id, session_id).classify(clean) == "equity":
            kw = _TF_KEYWORDS.get(tf, "")
            if kw and kw not in clean.lower():
                clean = clean + " " + kw
    except Exception:
        pass

    # Immediately show loading indicator while the orchestrator processes
    loading_hist = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": LOADING_HTML},
    ]
    yield (
        _INPUT_BUSY,
        _SEND_BUSY,
        loading_hist,
        gr.update(value=None, visible=False),
        suggestions,
    ) + _make_btn_updates(suggestions)

    chart_fig = None
    new_sugg = suggestions
    text = ""
    tab_used: str | None = None
    try:
        orch = get_orchestrator(user_id, session_id)
        tab_used = "equity"
        # Equity Q&A streams token-by-token (see EquityAgent.process_stream);
        # every other specialist yields exactly once with its finished
        # result, so this loop renders identically for both — it just runs
        # once for a non-streaming tab.
        # Download links are read after the loop (final yield), hence B007.
        steps: list[str] = []
        for out in orch.process_stream(clean, progress=True):
            if isinstance(out, ProgressUpdate):
                # UX-02: the research steps so far, under the loading dots.
                steps.append(out.label)
                yield (
                    _INPUT_BUSY,
                    _SEND_BUSY,
                    history
                    + [
                        {"role": "user", "content": message},
                        {"role": "assistant", "content": _progress_html(steps)},
                    ],
                    gr.update(),
                    suggestions,
                ) + _make_btn_updates(suggestions)
                continue
            text, chart, pdf_url, md_url, csv_url, tab_used = out
            if chart is not None:
                chart_fig = chart
            streaming_hist = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": _routing_badge(tab_used) + text},
            ]
            yield (
                _INPUT_BUSY,
                _SEND_BUSY,
                streaming_hist,
                gr.update(value=chart_fig, visible=chart_fig is not None),
                suggestions,
            ) + _make_btn_updates(suggestions)

        new_sugg = orch.generate_suggestions(text, clean)
        text = _routing_badge(tab_used) + text
        # OPS-02: links to /download/... routes, which any replica can serve.
        # Links live in the answer itself: a separate component's update raced
        # the sidebar re-render and was often lost (TEST-04), and in the
        # message they stay usable from the chat history.
        text += _download_links(pdf_url, md_url, csv_url)
    except Exception as e:
        # SEC-08: the user gets a friendly message and a short error ID; the
        # exception text (which can carry SQL, paths, provider responses) goes
        # only to the log. OBS-02: the error ID is the turn's id prefix — it
        # finds the `turns` row and the trace (scripts/find_turn.py).
        from utils.logging_setup import turn_id_var

        error_id = error_id_from_turn(turn_id_var.get())
        if isinstance(e, UserInputError | ConfigurationError):
            log.info(f"[respond] turn refused ({type(e).__name__}, error_id={error_id})")
        else:
            log.exception(f"[respond] turn failed (error_id={error_id})")
        text = render_error(e if isinstance(e, AriaError) else None, error_id)
        if isinstance(e, UnsafeURLError):
            # A saved BYO base URL that fails the egress policy (SEC-01).
            text += "\n\nUpdate or remove the base URL in the **Settings** tab."

    # A save failure here (e.g. a Neon connection dropped for being idle —
    # utils/db.py now retries once, but a second failure is still possible)
    # must never stop the terminal yield below from firing: that yield is
    # what re-enables the input box (_INPUT_READY/_SEND_READY, the
    # Finding-1 fix). Losing one turn's chat-history row is a much smaller
    # problem than leaving the whole input permanently locked.
    try:
        _persist_turn(user_id, session_id, message, text, is_first=not history, route=tab_used)
    except Exception as e:
        log.warning(f"[respond] failed to persist turn for session {session_id}: {e}")

    updated_history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": text},
    ]
    chart_update = gr.update(value=chart_fig, visible=chart_fig is not None)
    btn_updates = _make_btn_updates(new_sugg)
    log.info(f"[respond] turn end ms={int((time.monotonic() - started) * 1000)}")
    yield (
        _INPUT_READY,
        _SEND_READY,
        updated_history,
        chart_update,
        new_sugg,
    ) + btn_updates


def respond_exclusive(user_id, message, history, suggestions, session_id, tf):
    """respond(), but at most one turn per user at a time (REL-01), with every
    log line of the turn tagged by run_in_turn (OBS-01)."""
    if lifecycle.is_draining():
        # OPS-04: this instance is shutting down; don't start work it would
        # have to cut off. The message survives in the box for a resend.
        log.info("[respond] rejected: server draining for shutdown")
        yield _busy_outputs(message, history, suggestions, text=_DRAINING_TEXT)
        return
    if not _turn_allowed(user_id):
        log.info("[respond] rejected: per-user turn rate limit")
        yield _busy_outputs(message, history, suggestions, text=_RATE_LIMITED_TEXT)
        return
    lock = _user_turn_lock(user_id, session_id)
    if not lock.acquire(blocking=False):
        log.info("[respond] rejected: user already has a turn in flight")
        yield _busy_outputs(message, history, suggestions)
        return
    try:
        # run_in_turn keeps one turn_id on every log line of this turn, across
        # yields (Gradio resumes each generator step in a fresh context).
        with lifecycle.track_turn():
            yield from run_in_turn(
                respond(user_id, message, history, suggestions, session_id, tf), user_id, session_id
            )
    finally:
        lock.release()


_BUSY_TEXT = "⏳ Still working on your previous question — send this again once that answer arrives."
_DRAINING_TEXT = "🔄 ARIA is restarting for an update. Please send this again in a minute."
_RATE_LIMITED_TEXT = (
    f"🐢 You're sending messages faster than ARIA can answer them (limit: {TURNS_PER_MINUTE} a minute). "
    "Please wait a moment and send this again."
)


def _turn_allowed(user_id) -> bool:
    """SEC-12: at most TURNS_PER_MINUTE turns per user per minute, counted in
    Postgres so every replica shares the budget. Fails open if the database
    is unreachable (the turn itself would then fail with a clear error)."""
    if not user_id:
        return True
    from utils.db import rate_limit_take

    try:
        return rate_limit_take(f"turn:{user_id}", TURNS_PER_MINUTE, 60)
    except Exception:
        log.warning("[respond] rate-limit check failed; allowing the turn", exc_info=True)
        return True


def _busy_outputs(message, history: list, suggestions: list, text: str = _BUSY_TEXT) -> tuple:
    """Reply for a message that can't start now: the user's previous turn is
    still running (e.g. another browser tab), or the server is draining for a
    restart (OPS-04). Keeps their text in the box."""
    busy_hist = (history or []) + [
        {"role": "user", "content": (message or "")[:200]},
        {"role": "assistant", "content": text},
    ]
    keep_text = gr.update(value=message or "", interactive=True)
    return (keep_text, _SEND_READY, busy_hist, gr.update(), suggestions) + tuple(
        gr.update() for _ in range(4)
    )


def _persist_turn(
    user_id: int, session_id: int, user_msg: str, assistant_msg: str, is_first: bool, route: str | None = None
):
    """Saves the exchange. The assistant row carries its turn id (feedback and
    traces link to it, LLM-12) and the route in `meta`; the stored text has no
    routing badge, so it never reaches the model as history."""
    from agents.conversation_memory import strip_badge
    from utils.db import chat_save, session_touch
    from utils.logging_setup import turn_id_var

    meta = {"turn_id": turn_id_var.get() or None, "route": route}
    chat_save(user_id, session_id, "chat", "user", user_msg)
    chat_save(user_id, session_id, "chat", "assistant", strip_badge(assistant_msg), meta=meta)
    session_touch(user_id, session_id, title=_session_title(user_msg) if is_first else None)


def clear_chat(user_id: int):
    # "Clear" starts a new chat; the old one stays saved with its own state.
    return ([], gr.update(value=None, visible=False), CHAT_DEFAULTS) + _make_btn_updates(CHAT_DEFAULTS)


def _make_btn_updates(sugg: list) -> tuple:
    updates = []
    for i in range(4):
        if i < len(sugg):
            updates.append(gr.update(value=sugg[i], visible=True))
        else:
            updates.append(gr.update(value="", visible=False))
    return tuple(updates)


# ── ChatGPT-style sidebar: past sessions per user ──────────────────────────────


def _build_chat_sidebar(
    tab_id,
    user_state,
    session_state,
    refresh_state,
    chatbot,
    chart_panel,
    suggestions_state,
    sug_btns,
    extra_reset=None,
):
    """
    ChatGPT-style left sidebar: a persistent icon rail (toggle + new-chat
    icon) that's always visible, plus a full nav panel (session list) that
    collapses away — the rail stays, so the sidebar narrows to icons
    instead of disappearing entirely. Clicking a session loads its saved
    messages into the chatbot and resumes every specialist agent's
    conversation memory (the orchestrator doesn't know in advance which
    one(s) an old conversation touched). "+ New chat" detaches from the
    current session without deleting it, so the next message starts a
    fresh thread. `extra_reset` is any extra output components (download
    buttons etc.) that should hide when switching sessions.
    Returns (sidebar_col, sidebar_open_state, toggle_btn) so the caller can
    read/drive the collapse state from elsewhere if needed.
    """
    from utils.db import chat_load, session_delete, session_list

    extra_reset = extra_reset or []
    load_outputs = [session_state, chatbot, chart_panel, suggestions_state] + sug_btns + extra_reset
    sidebar_open = gr.State(True)

    def _blank_update():
        core = [None, [], gr.update(value=None, visible=False), CHAT_DEFAULTS] + list(
            _make_btn_updates(CHAT_DEFAULTS)
        )
        extras = [gr.update(value=None, visible=False) for _ in extra_reset]
        return tuple(core + extras)

    def _new_chat(user_id):
        # A new chat gets its own session (and orchestrator) on its first message.
        return _blank_update()

    def _load_session(user_id, sid):
        msgs = chat_load(user_id, sid)
        hist = [{"role": m["role"], "content": m["content"]} for m in msgs]
        # The session's orchestrator restores its own memory (and any pending
        # confirmation) from the database (REL-03/VAL-10).
        get_orchestrator(user_id, sid)
        core = [sid, hist, gr.update(value=None, visible=False), CHAT_DEFAULTS] + list(
            _make_btn_updates(CHAT_DEFAULTS)
        )
        extras = [gr.update(value=None, visible=False) for _ in extra_reset]
        return tuple(core + extras)

    def _delete_session(user_id, sid, current_sid, refresh):
        session_delete(user_id, sid)
        if sid == current_sid:
            result = _blank_update()
        else:
            no_change = [gr.update() for _ in range(len(load_outputs) - 1)]
            result = tuple([current_sid] + no_change)
        return result + (refresh + 1,)

    with gr.Column(scale=0, min_width=64, elem_classes=["chat-sidebar"]) as sidebar_col:
        # Icon rail — always visible in both expanded and collapsed states,
        # exactly like ChatGPT's persistent left rail.
        with gr.Row(elem_classes=["sidebar-rail"]):
            toggle_btn = gr.Button("◧", size="sm", elem_classes=["sidebar-icon-btn"], min_width=32)
            new_chat_icon_btn = gr.Button(
                "✎", size="sm", elem_classes=["sidebar-icon-btn", "collapsed-only"], min_width=32
            )

        # Expanded content — full "New chat" button + session list.
        with gr.Column(visible=True, elem_classes=["sidebar-expanded-panel"]) as expanded_panel:
            new_chat_btn = gr.Button("＋ New chat", size="sm", elem_classes=["new-chat-btn"])
            gr.Markdown("**Chats**", elem_classes=["sidebar-title"])

            # session_state is in the render inputs so the active chat gets a
            # highlighted row and the list re-renders the moment it changes.
            @gr.render(inputs=[user_state, refresh_state, session_state])
            def _render_sessions(user_id, _refresh, active_sid):
                if not user_id:
                    return
                sessions = session_list(user_id, tab_id)
                if not sessions:
                    gr.Markdown("*No past chats yet*", elem_classes=["sidebar-empty"])
                    return
                for s in sessions:
                    sid = s["id"]
                    row_classes = ["session-row"]
                    btn_classes = ["session-btn"] + (["session-btn-active"] if sid == active_sid else [])
                    with gr.Row(elem_classes=row_classes):
                        b = gr.Button(s["title"] or "New chat", size="sm", elem_classes=btn_classes)
                        d = gr.Button("✕", size="sm", scale=0, min_width=28, elem_classes=["session-del-btn"])
                    b.click(
                        fn=lambda uid=user_id, s=sid: _load_session(uid, s),
                        inputs=[],
                        outputs=load_outputs,
                        show_progress="hidden",
                    )
                    d.click(
                        fn=lambda current_sid, refresh, uid=user_id, s=sid: _delete_session(
                            uid, s, current_sid, refresh
                        ),
                        inputs=[session_state, refresh_state],
                        outputs=load_outputs + [refresh_state],
                        show_progress="hidden",
                    )

    new_chat_btn.click(_new_chat, inputs=[user_state], outputs=load_outputs, show_progress="hidden")
    new_chat_icon_btn.click(_new_chat, inputs=[user_state], outputs=load_outputs, show_progress="hidden")

    def _toggle(is_open):
        new_state = not is_open
        rail_class = ["chat-sidebar"] if new_state else ["chat-sidebar", "collapsed"]
        return new_state, gr.update(visible=new_state), gr.update(elem_classes=rail_class)

    toggle_btn.click(_toggle, [sidebar_open], [sidebar_open, expanded_panel, sidebar_col])

    return sidebar_col, sidebar_open, toggle_btn


# ── CSS ───────────────────────────────────────────────────────────────────────
APP_CSS = """
/* UX-03: every colour is a token. Light by default, dark when Gradio's
   `.dark` is on <body> (set from the stored choice or prefers-color-scheme,
   see APP_JS). Pairs used for text are checked for WCAG AA (4.5:1) in
   tests/test_a11y_tokens.py; text is never smaller than 12px. */
:root {
    --aria-bg: #ffffff;
    --aria-surface: #f6f8fa;
    --aria-surface-2: #eaeef2;
    --aria-sunken: #f6f8fa;
    --aria-border: #d0d7de;
    --aria-text: #1f2328;
    --aria-text-2: #31373d;
    --aria-muted: #59636e;
    --aria-accent: #0969da;
    --aria-accent-fg: #0550ae;
    --aria-accent-soft: #ddf4ff;
    --aria-accent-strong: #0969da;
    --aria-on-accent: #ffffff;
    --aria-accent-hover: #0550ae;
    --aria-danger: #cf222e;
    --aria-code: #0550ae;
    --aria-focus: #0969da;
}
.dark {
    --aria-bg: #0d1117;
    --aria-surface: #161b22;
    --aria-surface-2: #21262d;
    --aria-sunken: #0a0d12;
    --aria-border: #30363d;
    --aria-text: #e6edf3;
    --aria-text-2: #c9d1d9;
    --aria-muted: #9da7b3;
    --aria-accent: #58a6ff;
    --aria-accent-fg: #58a6ff;
    --aria-accent-soft: #132a45;
    --aria-accent-strong: #1f6feb;
    --aria-on-accent: #ffffff;
    --aria-accent-hover: #1158c7;
    --aria-danger: #ff7b72;
    --aria-code: #79c0ff;
    --aria-focus: #58a6ff;
}
body, .gradio-container {
    background: var(--aria-bg) !important;
    color: var(--aria-text) !important;
}
.gradio-container {
    max-width: 100% !important;
    padding: 0 !important;
    margin: 0 !important;
    /* Gradio's default primary blue (blue-500) under white text is 3.7:1;
       its theme variables are redefined here, closer than its own :root/.dark. */
    --button-primary-background-fill: var(--aria-accent-strong);
    --button-primary-background-fill-hover: var(--aria-accent-hover);
    --button-primary-border-color: var(--aria-accent-strong);
    --button-primary-border-color-hover: var(--aria-accent-hover);
    --button-primary-text-color: var(--aria-on-accent);
    --button-primary-text-color-hover: var(--aria-on-accent);
}
/* Keyboard focus is always visible (WCAG 2.4.7) */
.gradio-container :is(button, a, input, textarea, select, [tabindex]):focus-visible {
    outline: 2px solid var(--aria-focus) !important;
    outline-offset: 2px !important;
}
/* Tabs */
.tab-nav button {
    background: var(--aria-surface) !important;
    color: var(--aria-muted) !important;
    border: none !important;
    border-bottom: 2px solid transparent !important;
    padding: 10px 20px !important;
    font-size: 14px !important;
}
.tab-nav button.selected {
    color: var(--aria-accent-fg) !important;
    border-bottom: 2px solid var(--aria-accent) !important;
    background: var(--aria-bg) !important;
}
/* Chatbot */
.chatbot {
    background: var(--aria-bg) !important;
    border: none !important;
}
/* Message bubbles */
.message.user > div {
    background: var(--aria-surface) !important;
    border: 1px solid var(--aria-border) !important;
    border-radius: 12px 12px 4px 12px !important;
    color: var(--aria-text) !important;
}
.message.bot > div {
    background: transparent !important;
    color: var(--aria-text) !important;
}
.message.bot table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    background: var(--aria-surface);
    border-radius: 6px;
    overflow: hidden;
}
.message.bot th {
    background: var(--aria-surface-2);
    color: var(--aria-accent-fg);
    padding: 7px 12px;
    border-bottom: 1px solid var(--aria-border);
}
.message.bot td { padding: 6px 12px; border-bottom: 1px solid var(--aria-surface-2); }
.message.bot code { background: var(--aria-surface-2); color: var(--aria-code); padding: 1px 6px; border-radius: 4px; }
.message.bot a { color: var(--aria-accent-fg); text-decoration: underline; }
/* Input */
.input-row textarea {
    background: var(--aria-surface) !important;
    border: 1px solid var(--aria-border) !important;
    color: var(--aria-text) !important;
    border-radius: 10px !important;
}
.input-row textarea:focus { border-color: var(--aria-accent) !important; }
/* Suggestion chips */
.sugg-btn {
    background: var(--aria-surface) !important;
    color: var(--aria-text-2) !important;
    border: 1px solid var(--aria-border) !important;
    border-radius: 16px !important;
    font-size: 13px !important;
    padding: 4px 12px !important;
}
.sugg-btn:hover {
    color: var(--aria-accent-fg) !important;
    border-color: var(--aria-accent) !important;
}
/* Send / Clear buttons */
.send-btn { background: var(--aria-accent-strong) !important; color: var(--aria-on-accent) !important; border-radius: 8px !important; border: none !important; }
.send-btn:hover { background: var(--aria-accent-hover) !important; }
.clear-btn { background: transparent !important; color: var(--aria-muted) !important; border: 1px solid var(--aria-border) !important; border-radius: 8px !important; }
.clear-btn:hover { color: var(--aria-danger) !important; border-color: var(--aria-danger) !important; }
/* Top header */
#aria-header {
    background: var(--aria-surface);
    border-bottom: 1px solid var(--aria-border);
    padding: 8px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
}
.aria-brand { font-size: 18px; font-weight: 700; color: var(--aria-accent-fg); }
.aria-tagline { font-size: 12px; color: var(--aria-muted); margin-left: 10px; }
.aria-pill {
    font-size: 12px;
    padding: 3px 10px;
    border-radius: 12px;
    background: var(--aria-accent-soft);
    color: var(--aria-accent-fg);
    border: 1px solid var(--aria-border);
}
.aria-footer { text-align: center; font-size: 12px; color: var(--aria-muted); padding: 6px 0 10px; }
footer { display: none !important; }
/* UX-02: research steps under the loading dots */
.aria-progress { font-size: 12px; color: var(--aria-muted); margin-top: 6px; line-height: 1.5; }
.aria-progress strong { color: var(--aria-text-2); font-weight: 500; }
/* Typing / loading indicator */
@keyframes aria-bounce {
    0%, 80%, 100% { transform: translateY(0);    opacity: 0.35; }
    40%            { transform: translateY(-5px); opacity: 1;    }
}
.aria-dot {
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--aria-accent);
    margin: 0 2px;
    vertical-align: middle;
    animation: aria-bounce 1.2s infinite ease-in-out;
}
.aria-dot:nth-child(2) { animation-delay: 0.2s; }
.aria-dot:nth-child(3) { animation-delay: 0.4s; }
@media (prefers-reduced-motion: reduce) {
    .aria-dot { animation: none; opacity: 0.8; }
}
/* Timeframe radio */
.tf-radio label { font-size: 12px !important; color: var(--aria-text) !important; }
.tf-radio .wrap { gap: 4px !important; }
/* Chat sidebar (ChatGPT-style) */
.sidebar-title { color: var(--aria-muted) !important; font-size: 12px !important; padding: 2px 4px !important; }
.new-chat-btn {
    background: var(--aria-surface) !important;
    color: var(--aria-text) !important;
    border: 1px solid var(--aria-border) !important;
    border-radius: 8px !important;
    font-size: 13px !important;
    margin-bottom: 6px !important;
    width: 100% !important;
}
.new-chat-btn:hover { border-color: var(--aria-accent) !important; }
.sidebar-empty { color: var(--aria-muted) !important; font-size: 12px !important; padding: 6px 4px !important; }
.session-row { gap: 4px !important; margin-bottom: 2px !important; }
.session-btn {
    background: transparent !important;
    color: var(--aria-text-2) !important;
    border: none !important;
    text-align: left !important;
    justify-content: flex-start !important;
    font-size: 13px !important;
    padding: 6px 8px !important;
    border-radius: 6px !important;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
}
.session-btn:hover { background: var(--aria-surface) !important; color: var(--aria-accent-fg) !important; }
.session-btn-active {
    background: var(--aria-surface) !important;
    color: var(--aria-accent-fg) !important;
    border-left: 2px solid var(--aria-accent) !important;
    font-weight: 600 !important;
}
.session-del-btn {
    background: transparent !important;
    color: var(--aria-muted) !important;
    border: none !important;
    font-size: 12px !important;
    padding: 4px !important;
}
.session-del-btn:hover { color: var(--aria-danger) !important; }
/* ChatGPT-style chat shell: sidebar docked left, centered chat column */
.chat-shell { gap: 0 !important; align-items: stretch !important; }
.chat-main {
    max-width: 820px !important;
    margin: 0 auto !important;
}
/* order:-1 pins the sidebar to the visual left of the flex row regardless
   of build order — it has to be built after chatbot/chart_panel/sug_btns
   exist (its click handlers target them), but must render first. */
.chat-sidebar {
    order: -1;
    background: var(--aria-sunken) !important;
    border-right: 1px solid var(--aria-border) !important;
    padding: 8px 6px !important;
    width: 260px !important;
    min-width: 260px !important;
    max-width: 260px !important;
    transition: width .15s ease, min-width .15s ease, max-width .15s ease, padding .15s ease;
    overflow-x: hidden;
    overflow-y: auto;
    max-height: 720px;
}
.chat-sidebar.collapsed {
    width: 52px !important;
    min-width: 52px !important;
    max-width: 52px !important;
    padding: 8px 6px !important;
}
.sidebar-rail {
    gap: 4px !important;
    margin-bottom: 8px !important;
}
.sidebar-icon-btn {
    background: transparent !important;
    color: var(--aria-text-2) !important;
    border: 1px solid transparent !important;
    border-radius: 8px !important;
    font-size: 15px !important;
    padding: 6px !important;
    width: 32px !important;
    min-width: 32px !important;
    max-width: 32px !important;
    flex: 0 0 auto !important;
}
.sidebar-icon-btn:hover { background: var(--aria-surface) !important; border-color: var(--aria-border) !important; }
/* The icon-only "new chat" button only makes sense once the full panel
   (with its own "+ New chat" button) is hidden — CSS keeps it out of the
   way in the expanded state without needing a second server round-trip. */
.chat-sidebar:not(.collapsed) .collapsed-only { display: none !important; }
.chat-sidebar.collapsed .sidebar-expanded-panel { display: none !important; }
/* Watchlist table (plain HTML — see _watchlist_table_html for why) */
.wl-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    background: var(--aria-surface);
    border-radius: 6px;
    overflow: hidden;
}
.wl-table th {
    background: var(--aria-surface-2);
    color: var(--aria-accent-fg);
    padding: 8px 12px;
    text-align: left;
    border-bottom: 1px solid var(--aria-border);
}
.wl-table td {
    padding: 7px 12px;
    border-bottom: 1px solid var(--aria-surface-2);
    color: var(--aria-text);
}
.wl-table tr:last-child td { border-bottom: none; }
.wl-empty { color: var(--aria-muted); font-size: 13px; padding: 10px 2px; }
"""


CSS = APP_CSS  # older name

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
  };
  new MutationObserver(() => {
    if (!queued) { queued = true; requestAnimationFrame(label); }
  }).observe(document.body, {childList: true, subtree: true});
  mq.addEventListener('change', apply);
  apply();
  label();
})()"""

THEME_TOGGLE_JS = "() => { window.ariaToggleTheme && window.ariaToggleTheme(); }"

HEADER_HTML = """
<header id='aria-header'>
  <div>
    <span class='aria-brand'>⚡ ARIA</span>
    <span class='aria-tagline'>Autonomous Research &amp; Investment Analyst · NSE · BSE · v2</span>
  </div>
  <div class='aria-pill'>Free trial · bring your own key for unlimited use (Settings)</div>
</header>
"""

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


# ── Build the single unified chat tab (orchestrator-routed) ──────────────────


def onboarding_card(info: dict) -> str:
    """UX-01 first-run card: 3 things ARIA does, its main limit, the trial and Telegram."""
    from config import TRIAL_DAILY_LIMIT

    if info.get("byo"):
        trial = "You're using your own LLM key, so there's no daily limit."
    else:
        left = max(0, TRIAL_DAILY_LIMIT - int(info.get("used_today") or 0))
        trial = (
            f"**{left} of {TRIAL_DAILY_LIMIT}** free questions left today. "
            "Add your own LLM key in **⚙️ Settings** for unlimited use."
        )
    telegram = (
        "✅ Telegram is connected: alerts reach your phone."
        if info.get("telegram")
        else "To get alerts on your phone: **⚙️ Settings → Connect Telegram** (one tap)."
    )
    return (
        "### 👋 Welcome to ARIA\n"
        "1. **Research any NSE/BSE company**: try *“Analyse TCS”* or *“Compare TCS with peers”*.\n"
        "2. **Set alerts** on prices, % moves or indicators: *“Alert me if INFY falls 5%”*.\n"
        "3. **Plan and compare funds**: *“Best ELSS for tax saving”* or *“Plan ₹50L for retirement”*.\n\n"
        "**Good to know:** ARIA explains and researches; it doesn't give personal buy/sell advice, "
        "and prices can be delayed.\n\n"
        f"{trial}  \n{telegram}"
    )


def _build_chat_tab(user_state: gr.State):
    """
    One chat surface for everything — equity research, report generation,
    mutual funds, screening. The orchestrator (agents/orchestrator.py)
    decides which specialist agent handles each message; the user never
    picks a tab for it.
    """
    tab_id = "chat"

    session_state = gr.State(None)
    refresh_state = gr.State(0)
    suggestions_state = gr.State(CHAT_DEFAULTS)

    # Sidebar has to be *built* after chatbot/chart_panel/sug_btns exist (it
    # wires click handlers that target them), but must *render* on the left
    # like ChatGPT — solved with `order: -1` in CSS (.chat-sidebar) rather
    # than fighting component build order.
    with gr.Row(elem_classes=["chat-shell"]):
        with gr.Column(scale=4, elem_classes=["chat-main"]):
            # UX-01: first-run card (hidden once dismissed) and the capability boundary.
            with gr.Group(visible=False, elem_id="onboarding") as onboarding_box:
                onboarding_md = gr.Markdown("", elem_id="onboarding-card")
                onboarding_done = gr.Button("Got it — let's start", size="sm", variant="primary")
            with gr.Accordion("What ARIA can and can't do", open=False, elem_id="capabilities"):
                gr.Markdown(CAPABILITIES_MD)
            chatbot = gr.Chatbot(
                label="",
                show_label=False,
                # SEC-10: stated explicitly so a Gradio default change can't
                # turn off sanitising of LLM- and user-supplied markup.
                sanitize_html=True,
                height=480,
                render_markdown=True,
                elem_id=f"chatbot-{tab_id}",
            )
            chart_panel = gr.Plot(value=None, elem_id=f"chart-{tab_id}", show_label=False, visible=False)

            # LLM-12: a thumbs-down opens this so the user can say what was wrong.
            feedback_turn = gr.State(None)
            with gr.Group(visible=False, elem_id=f"feedback-{tab_id}") as feedback_box:
                feedback_reason = gr.Radio(
                    ["A number is wrong", "Out of date", "Not what I asked", "Other"],
                    label="What was wrong with this answer?",
                )
                feedback_text = gr.Textbox(label="Details (optional)", max_lines=3, max_length=2000)
                with gr.Row():
                    feedback_send = gr.Button("Send feedback", size="sm", variant="primary")
                    feedback_skip = gr.Button("Skip", size="sm")
            feedback_note = gr.Markdown("", elem_id=f"feedback-note-{tab_id}")

            # Timeframe selector — only applies when the orchestrator routes to equity
            with gr.Row(elem_classes=["tf-radio"]):
                tf_radio = gr.Radio(
                    choices=["1D", "1W", "1M", "3M", "6M", "2Y"],
                    value="1D",
                    label="Chart timeframe (equity queries)",
                    interactive=True,
                    scale=1,
                )

            # Download links — report PDF/MD and screener CSV, hidden until
            # produced. OPS-02: plain links to /download/... (any replica
            # serves them), not Gradio file components tied to one replica.

            gr.Markdown("**Suggested:**", elem_id=f"sugg-label-{tab_id}")
            with gr.Row():
                sug_btns = [
                    gr.Button(CHAT_DEFAULTS[i], size="sm", elem_classes=["sugg-btn"]) for i in range(4)
                ]

            # VAL-08: shown only while the research graph waits for a yes/no.
            with gr.Row(visible=False, elem_id=f"confirm-row-{tab_id}") as confirm_row:
                yes_btn = gr.Button("✅ Yes, go ahead", variant="primary", size="sm")
                no_btn = gr.Button("✖ No, cancel", variant="secondary", size="sm")

            with gr.Row(elem_id=f"input-row-{tab_id}"):
                msg_box = gr.Textbox(
                    placeholder=CHAT_PLACEHOLDER,
                    label="",
                    show_label=False,
                    lines=1,
                    max_lines=3,
                    autofocus=True,
                    elem_id=f"msg-{tab_id}",
                    scale=10,
                    max_length=MAX_CHAT_CHARS,  # UI hint; respond() enforces it (VAL-01)
                )
                send_btn = gr.Button(
                    "Send ⚡", variant="primary", scale=1, min_width=80, elem_classes=["send-btn"]
                )
                clear_btn = gr.Button("Clear chat", scale=1, min_width=60, elem_classes=["clear-btn"])

        _build_chat_sidebar(
            tab_id,
            user_state,
            session_state,
            refresh_state,
            chatbot,
            chart_panel,
            suggestions_state,
            sug_btns,
        )

    outputs = (
        [msg_box, send_btn, chatbot, chart_panel, suggestions_state]
        + sug_btns
        + [session_state, refresh_state]
    )

    def _respond(user_id, session_id, refresh, msg, hist, sugg, tf, request: gr.Request):
        # SEC-04: the authenticated request decides who is chatting (API
        # clients never run the page's load event that fills user_state).
        user_id = _request_user(request) or user_id
        if not user_id:
            return
        if session_id is None and isinstance(clean_message(msg), CleanMessage):
            from utils.db import session_create

            session_id = session_create(user_id, tab_id, title=_session_title(msg))
            # A freshly created session must show up in the sidebar the moment
            # this turn starts, not on some later yield — bump refresh_state
            # right away instead of leaving it to the loop below (see
            # ARCHITECTURE.md: the sidebar's @gr.render previously missed a
            # brand-new session for the rest of that browser session).
            refresh += 1
        for i, out in enumerate(respond_exclusive(user_id, msg, hist, sugg, session_id, tf)):
            yield out + (session_id, refresh if i == 0 else refresh + 1)

    def _clear(user_id):
        hist, chart, sugg, *btns = clear_chat(user_id)
        return [hist, chart, sugg] + list(btns) + [None]

    # show_progress="hidden" on both: Gradio's own generic ETA overlay
    # ("processing | Xs/Ys" text + a stray link icon) otherwise renders on
    # top of the chat area and even inside the input box once a turn runs
    # long — most visibly under LLM-provider rate-limiting, exactly when the
    # app most needs to look composed. respond()'s own LOADING_HTML
    # (bouncing dots, prefixed to the assistant bubble) is the one loading
    # indicator meant to be visible; this stops Gradio's from fighting it.
    chat_inputs = [user_state, session_state, refresh_state, msg_box, chatbot, suggestions_state, tf_radio]

    # Both triggers share one "chat" worker pool (REL-01): the limit is on
    # concurrent chat turns server-wide, not per button.
    def _confirm_visible(user_id, session_id, request: gr.Request):
        """Show Yes/No while this chat's graph is paused for confirmation."""
        user_id = _request_user(request) or user_id
        orch = _orchestrators.get((user_id, session_id)) if user_id else None
        return gr.update(visible=bool(orch and getattr(orch, "_awaiting_confirmation", False)))

    def _on_like(data: gr.LikeData, user_id, session_id, request: gr.Request):
        """Thumbs up/down on an answer, linked to the turn that produced it."""
        user_id = _request_user(request) or user_id
        hidden = (gr.update(visible=False), None, "")
        if not user_id or session_id is None:
            return hidden
        from utils.db import chat_load, feedback_record

        index = data.index[0] if isinstance(data.index, (list, tuple)) else data.index
        rows = chat_load(user_id, session_id)
        if not isinstance(index, int) or index >= len(rows) or rows[index]["role"] != "assistant":
            return hidden
        turn_id = (rows[index].get("meta") or {}).get("turn_id")
        if not turn_id or data.liked is None:
            return hidden
        rating = 1 if data.liked else -1
        feedback_record(user_id, turn_id, rating, session_id=session_id)
        from utils.metrics import observe_feedback

        observe_feedback(rating)
        if rating == 1:
            return gr.update(visible=False), None, "👍 Thanks for the feedback."
        return gr.update(visible=True), turn_id, ""

    def _send_feedback(user_id, session_id, turn_id, reason, details, request: gr.Request):
        user_id = _request_user(request) or user_id
        if user_id and turn_id:
            from utils.db import feedback_record

            feedback_record(user_id, turn_id, -1, session_id=session_id, reason=reason, free_text=details)
        return gr.update(visible=False), None, None, "", "🙏 Thanks — this helps us fix wrong answers."

    chatbot.like(_on_like, [user_state, session_state], [feedback_box, feedback_turn, feedback_note])
    feedback_send.click(
        _send_feedback,
        [user_state, session_state, feedback_turn, feedback_reason, feedback_text],
        [feedback_box, feedback_turn, feedback_reason, feedback_text, feedback_note],
    )
    feedback_skip.click(lambda: (gr.update(visible=False), None), None, [feedback_box, feedback_turn])

    def _onboarding_view(user_id, request: gr.Request):
        """UX-01: the first-run card for users who haven't dismissed it."""
        user_id = _request_user(request) or user_id
        if not user_id:
            return gr.update(visible=False), ""
        from datetime import datetime

        from utils.db import user_onboarding
        from utils.helpers import IST

        try:
            info = user_onboarding(user_id, datetime.now(IST).date())
        except Exception:
            log.warning("[onboarding] status unavailable", exc_info=True)
            return gr.update(visible=False), ""
        if info["onboarded"]:
            return gr.update(visible=False), ""
        return gr.update(visible=True), onboarding_card(info)

    def _onboarding_dismiss(user_id, request: gr.Request):
        user_id = _request_user(request) or user_id
        if user_id:
            from utils.db import user_mark_onboarded

            user_mark_onboarded(user_id)
        return gr.update(visible=False)

    user_state.change(_onboarding_view, [user_state], [onboarding_box, onboarding_md])
    onboarding_done.click(_onboarding_dismiss, [user_state], [onboarding_box])

    chat_kwargs = {"show_progress": "hidden", "concurrency_id": "chat", "concurrency_limit": CHAT_CONCURRENCY}
    msg_box.submit(_respond, chat_inputs, outputs, **chat_kwargs).then(
        _confirm_visible, [user_state, session_state], [confirm_row]
    )
    send_btn.click(_respond, chat_inputs, outputs, **chat_kwargs).then(
        _confirm_visible, [user_state, session_state], [confirm_row]
    )
    for btn, answer in ((yes_btn, "yes"), (no_btn, "no")):
        # A click is the typed answer: fill the box, run the same turn path.
        btn.click(lambda a=answer: a, None, [msg_box]).then(
            _respond, chat_inputs, outputs, **chat_kwargs
        ).then(_confirm_visible, [user_state, session_state], [confirm_row])
    clear_btn.click(
        _clear,
        [user_state],
        [chatbot, chart_panel, suggestions_state] + sug_btns + [session_state],
        show_progress="hidden",
    )

    for btn in sug_btns:
        btn.click(
            fn=lambda btn_val: (btn_val or "").strip(),
            inputs=[btn],
            outputs=[msg_box],
        )

    return chatbot, chart_panel, session_state, suggestions_state


# ── Watchlist helpers ─────────────────────────────────────────────────────────


def _fetch_watchlist_table(user_id: int) -> list[list]:
    """Return rows for the watchlist dataframe: Symbol, Name, Price, Change%, RSI, 52W High, 52W Low."""
    from tools.analysis.technical import compute_all, snapshot
    from tools.data.fetcher import get_info, get_ohlcv
    from utils.db import watchlist_all

    symbols = watchlist_all(user_id)
    if not symbols:
        return []

    rows = []
    for sym in symbols:
        try:
            info = get_info(sym)
            price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
            prev = info.get("previousClose") or price
            chg = round(((price - prev) / prev) * 100, 2) if prev else 0.0
            name = (info.get("shortName") or sym)[:22]
            high52 = info.get("fiftyTwoWeekHigh", "N/A")
            low52 = info.get("fiftyTwoWeekLow", "N/A")

            # RSI from cached OHLCV (fast — usually already in Parquet cache).
            # snapshot() always has the "RSI" key but its value is None when
            # the underlying OHLCV history is too short/unavailable — a bare
            # .get("RSI", "—") only covers a missing key, not a None value,
            # so the table used to render the literal string "None".
            rsi = "—"
            try:
                df = get_ohlcv(sym, "3mo", "1d")
                if not df.empty:
                    ta = snapshot(compute_all(df.copy()))
                    rsi = ta.get("RSI")
                    rsi = "N/A" if rsi is None else rsi
            except Exception:
                pass

            arrow = "▲" if chg >= 0 else "▼"
            rows.append(
                [
                    sym,
                    name,
                    f"₹{price:,.2f}",
                    f"{arrow} {chg:+.2f}%",
                    rsi,
                    f"₹{high52:,.2f}" if isinstance(high52, float) else str(high52),
                    f"₹{low52:,.2f}" if isinstance(low52, float) else str(low52),
                ]
            )
        except Exception:
            rows.append([sym, "—", "—", "—", "—", "—", "—"])
    return rows


def _watchlist_table_html(rows: list[list]) -> str:
    """
    Renders the watchlist as a plain HTML table instead of gr.Dataframe.
    gr.Dataframe has a confirmed client-side rendering bug in this Gradio
    version: the server-sent payload always has the correct row_count and
    full data (verified over the wire via the SSE stream — every add/remove
    carried the right number of rows), but the browser only ever paints the
    first row, no matter whether the update goes through gr.update(),
    a fresh component instance, or a stable `key=` inside @gr.render(). A
    plain HTML table has no such virtualization/diffing layer to misbehave.
    """
    if not rows:
        return "<p class='wl-empty'>No symbols yet — add one below.</p>"
    # SEC-10: every cell is escaped — symbols come from users, names from yfinance.
    head = "".join(f"<th>{html_text(c)}</th>" for c in WATCHLIST_COLS)
    body = "".join("<tr>" + "".join(f"<td>{html_text(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table class='wl-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _build_watchlist_tab(user_state: gr.State):
    """
    Watchlist tab: add/remove symbols, live price table with RSI, refresh
    button. The table lives inside @gr.render() keyed to a refresh counter
    (bumped on every add/remove/refresh) so it re-renders on every change —
    see _watchlist_table_html() for why it's plain HTML, not gr.Dataframe.
    """
    from utils.db import watchlist_add, watchlist_remove
    from utils.helpers import clean_ticker

    gr.Markdown("## 📌 Watchlist\nTrack live prices and key indicators for your saved symbols.")

    wl_refresh = gr.State(0)

    @gr.render(inputs=[user_state, wl_refresh])
    def _render_table(user_id, _refresh):
        rows = _fetch_watchlist_table(user_id) if user_id else []
        gr.HTML(_watchlist_table_html(rows), elem_id="wl-table")

    # Status message
    status = gr.Markdown("")

    # Add / Remove / Refresh row
    with gr.Row():
        add_box = gr.Textbox(
            placeholder="Add symbol (e.g. TCS, RELIANCE)",
            label="",
            show_label=False,
            scale=4,
            lines=1,
            max_lines=1,
        )
        add_btn = gr.Button("➕ Add", variant="primary", scale=1, min_width=80)
        rem_box = gr.Textbox(
            placeholder="Remove symbol", label="", show_label=False, scale=4, lines=1, max_lines=1
        )
        rem_btn = gr.Button("➖ Remove", variant="secondary", scale=1, min_width=80)
        ref_btn = gr.Button("🔄 Refresh", variant="secondary", scale=1, min_width=90)

    def _add(user_id, sym_raw, refresh):
        # VAL-07: only listed NSE symbols, resolved like a chat mention, max 50.
        from utils.db import WatchlistFull
        from utils.errors import UserInputError
        from utils.forms import WATCHLIST_MAX, watchlist_symbol

        try:
            sym = watchlist_symbol(sym_raw)
        except UserInputError as e:
            return refresh, f"⚠️ {html_text(e.user_message)}"
        try:
            added = watchlist_add(user_id, sym, max_symbols=WATCHLIST_MAX)
        except WatchlistFull:
            return refresh, f"⚠️ Your watchlist is full ({WATCHLIST_MAX} symbols). Remove one to add another."
        if not added:
            return refresh, f"ℹ️ **{sym}** is already on your watchlist."
        return refresh + 1, f"✅ **{sym}** added to watchlist."

    def _remove(user_id, sym_raw, refresh):
        sym = clean_ticker(sym_raw.strip().upper())
        if not sym:
            return refresh, "⚠️ Enter a symbol to remove."
        watchlist_remove(user_id, sym)
        return refresh + 1, f"🗑️ **{sym}** removed from watchlist."

    def _refresh_click(user_id, refresh):
        n = len(_fetch_watchlist_table(user_id))
        return refresh + 1, f"🔄 Refreshed {n} symbol{'s' if n != 1 else ''}."

    add_btn.click(_add, inputs=[user_state, add_box, wl_refresh], outputs=[wl_refresh, status])
    rem_btn.click(_remove, inputs=[user_state, rem_box, wl_refresh], outputs=[wl_refresh, status])
    ref_btn.click(
        _refresh_click,
        inputs=[user_state, wl_refresh],
        outputs=[wl_refresh, status],
        concurrency_limit=WATCHLIST_CONCURRENCY,
    )  # one yfinance call per symbol; keep it bounded
    add_box.submit(_add, inputs=[user_state, add_box, wl_refresh], outputs=[wl_refresh, status])

    return status


# ── Portfolio tab (UX-05) ──────────────────────────────────────────────────────

PORTFOLIO_LOT_COLS = ["Lot #", "Symbol", "Qty", "Avg price", "Buy date"]


def _portfolio_lots_html(lots: list[dict]) -> str:
    """Plain, escaped HTML table of lots (gr.Dataframe paints one row — see
    _watchlist_table_html)."""
    from utils.numbers import format_inr

    if not lots:
        return "<p class='wl-empty'>No holdings yet — add one below or import your broker's CSV.</p>"
    head = "".join(f"<th>{html_text(c)}</th>" for c in PORTFOLIO_LOT_COLS)
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{html_text(cell)}</td>"
            for cell in (
                lot["id"],
                lot["symbol"],
                f"{lot['qty'].normalize():f}",
                format_inr(lot["avg_price"]),
                lot["buy_date"] or "—",
            )
        )
        + "</tr>"
        for lot in lots
    )
    return f"<table class='wl-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _portfolio_view(user_id: int | None):
    """(summary markdown, chart, lots html) for the Portfolio tab."""
    from tools import portfolio
    from tools.charts.builder import portfolio_chart
    from utils.db import portfolio_all

    if not user_id:
        return "", None, _portfolio_lots_html([])
    lots = portfolio_all(user_id)
    holdings, summary = portfolio.valuation(lots)
    chart = portfolio_chart(portfolio.chart_rows(holdings)) if holdings else None
    return portfolio.to_markdown(holdings, summary), chart, _portfolio_lots_html(lots)


def _build_portfolio_tab(user_state: gr.State):
    """Portfolio tab: holdings at live prices (P&L, weights, allocation
    chart), add / edit / remove lots, and CSV import from a broker holdings
    export (Zerodha, Groww) or a plain sheet."""
    from tools import portfolio
    from utils.audit import audit
    from utils.db import (
        MAX_PORTFOLIO_LOTS,
        PortfolioFull,
        portfolio_add,
        portfolio_import,
        portfolio_remove,
        portfolio_remove_lot,
        portfolio_update,
    )
    from utils.errors import UserInputError

    gr.Markdown(
        "## 📊 Portfolio\nYour holdings at live prices. Quantities and prices are stored exactly; "
        "P&L is before tax and charges."
    )
    pf_refresh = gr.State(0)

    @gr.render(inputs=[user_state, pf_refresh])
    def _render(user_id, _refresh):
        summary_md, chart, lots_html = _portfolio_view(user_id)
        gr.Markdown(summary_md, elem_id="pf-summary")
        if chart is not None:
            gr.Plot(chart, show_label=False, elem_id="pf-chart")
        with gr.Accordion("Lots", open=False):
            gr.HTML(lots_html, elem_id="pf-lots")

    status = gr.Markdown("", elem_id="pf-status")

    with gr.Group():
        gr.Markdown("**Add a holding**")
        with gr.Row():
            add_sym = gr.Textbox(label="Company or NSE symbol", max_lines=1, max_length=60, scale=3)
            add_qty = gr.Textbox(label="Quantity", max_lines=1, max_length=30, scale=1)
            add_price = gr.Textbox(label="Avg buy price (₹)", max_lines=1, max_length=30, scale=1)
            add_date = gr.Textbox(label="Buy date (optional)", placeholder="YYYY-MM-DD", max_lines=1, scale=1)
            add_btn = gr.Button("➕ Add", variant="primary", scale=1, min_width=80)

    with gr.Accordion("Edit or remove", open=False):
        with gr.Row():
            lot_id = gr.Number(label="Lot #", precision=0, minimum=1, scale=1)
            ed_qty = gr.Textbox(label="New quantity", max_lines=1, max_length=30, scale=1)
            ed_price = gr.Textbox(label="New avg price (₹)", max_lines=1, max_length=30, scale=1)
            ed_date = gr.Textbox(label="Buy date", placeholder="YYYY-MM-DD", max_lines=1, scale=1)
        with gr.Row():
            upd_btn = gr.Button("✎ Update lot", size="sm")
            del_lot_btn = gr.Button("🗑 Remove lot", size="sm")
            del_sym = gr.Textbox(label="", placeholder="Symbol", show_label=False, max_lines=1, scale=2)
            del_sym_btn = gr.Button("🗑 Remove all lots of symbol", size="sm")

    with gr.Accordion("Import from your broker (CSV)", open=False):
        gr.Markdown(
            "Upload a holdings export: Zerodha Console (*Instrument, Qty., Avg. cost*), Groww "
            "(*Stock Name / ISIN, Quantity, Average buy price*) or any sheet with *symbol, quantity, "
            "avg price* (and optionally *buy date*) columns."
        )
        with gr.Row():
            csv_file = gr.File(label="Holdings CSV", file_types=[".csv"], scale=3)
            csv_replace = gr.Checkbox(label="Replace my current holdings", value=False, scale=1)
            csv_btn = gr.Button("⬆️ Import", variant="primary", scale=1)
    ref_btn = gr.Button("🔄 Refresh prices", size="sm")

    def _who(user_id, request):
        return _request_user(request) or user_id

    def _add(user_id, sym, qty, price, when, refresh, request: gr.Request):
        user_id = _who(user_id, request)
        if not user_id:
            return refresh, "⚠️ Please sign in."
        try:
            lot = portfolio.parse_lot(sym, qty, price, when)
            portfolio_add(user_id, lot["symbol"], lot["qty"], lot["avg_price"], lot["buy_date"])
        except UserInputError as e:
            return refresh, f"⚠️ {html_text(e.user_message)}"
        except PortfolioFull:
            return refresh, f"⚠️ You can keep up to {MAX_PORTFOLIO_LOTS} lots. Remove some to add more."
        return refresh + 1, f"✅ Added {lot['qty'].normalize():f} **{lot['symbol']}**."

    def _update(user_id, lid, qty, price, when, refresh, request: gr.Request):
        user_id = _who(user_id, request)
        try:
            ok = bool(lid) and portfolio_update(
                user_id,
                int(lid),
                portfolio.parse_quantity(qty),
                portfolio.parse_price(price),
                portfolio.parse_buy_date(when),
            )
        except UserInputError as e:
            return refresh, f"⚠️ {html_text(e.user_message)}"
        return (refresh + 1, f"✎ Lot #{int(lid)} updated.") if ok else (refresh, "⚠️ No such lot.")

    def _remove_lot(user_id, lid, refresh, request: gr.Request):
        user_id = _who(user_id, request)
        ok = bool(lid) and portfolio_remove_lot(user_id, int(lid))
        return (refresh + 1, f"🗑 Lot #{int(lid)} removed.") if ok else (refresh, "⚠️ No such lot.")

    def _remove_symbol(user_id, sym, refresh, request: gr.Request):
        user_id = _who(user_id, request)
        sym = (sym or "").strip().upper()
        n = portfolio_remove(user_id, sym) if sym else 0
        if not n:
            return refresh, "⚠️ You don't hold that symbol."
        return refresh + 1, f"🗑 Removed {n} lot{'s' if n != 1 else ''} of **{html_text(sym)}**."

    def _import(user_id, path, replace, refresh, request: gr.Request):
        user_id = _who(user_id, request)
        if not user_id:
            return refresh, "⚠️ Please sign in."
        if not path:
            return refresh, "⚠️ Choose a CSV file first."
        try:
            with open(path, "rb") as f:
                data = f.read(portfolio.MAX_CSV_BYTES + 1)
            lots, problems = portfolio.parse_csv(data)
            if not lots:
                raise UserInputError("No usable holdings in that file.")
            n = portfolio_import(user_id, lots, replace=bool(replace))
        except UserInputError as e:
            return refresh, f"⚠️ {html_text(e.user_message)}"
        except PortfolioFull:
            return refresh, f"⚠️ That would take you past {MAX_PORTFOLIO_LOTS} lots."
        audit("portfolio.import", actor_user_id=user_id, details={"lots": n, "replace": bool(replace)})
        msg = f"✅ Imported {n} holding{'s' if n != 1 else ''}" + (
            " (replaced the previous ones)." if replace else "."
        )
        if problems:
            msg += "\n\nSkipped:\n" + "\n".join(f"- {p}" for p in problems[:20])
        return refresh + 1, msg

    add_btn.click(_add, [user_state, add_sym, add_qty, add_price, add_date, pf_refresh], [pf_refresh, status])
    upd_btn.click(_update, [user_state, lot_id, ed_qty, ed_price, ed_date, pf_refresh], [pf_refresh, status])
    del_lot_btn.click(_remove_lot, [user_state, lot_id, pf_refresh], [pf_refresh, status])
    del_sym_btn.click(_remove_symbol, [user_state, del_sym, pf_refresh], [pf_refresh, status])
    csv_btn.click(_import, [user_state, csv_file, csv_replace, pf_refresh], [pf_refresh, status])
    ref_btn.click(
        lambda r: r + 1, [pf_refresh], [pf_refresh], concurrency_limit=WATCHLIST_CONCURRENCY
    )  # one quote per holding; keep it bounded
    return status


# ── Settings tab (bring-your-own LLM key) ──────────────────────────────────────


def _settings_status_md(user_id: int) -> str:
    from utils.db import _query, llm_config_get

    row = llm_config_get(user_id)
    if row:
        base = row["base_url"] or "api.openai.com (or provider default)"
        return f"✅ **Using your own key** — model `{row['model']}` via `{base}`."
    urow = _query(
        "SELECT free_msgs_used_today, free_msgs_reset_at FROM users WHERE id=%s",
        (user_id,),
        fetch="one",
    )
    used = urow["free_msgs_used_today"] if urow else 0
    from datetime import datetime

    from utils.helpers import IST

    today = datetime.now(IST).date()
    if urow and urow["free_msgs_reset_at"] != today:
        used = 0
    remaining = max(0, TRIAL_DAILY_LIMIT - used)
    return f"🆓 **Free trial** — {remaining}/{TRIAL_DAILY_LIMIT} messages left today. Add your own key below for unlimited use."


def _build_settings_tab(user_state: gr.State):
    """
    Bring-your-own LLM key: works with any OpenAI-compatible provider
    (OpenAI, Groq, OpenRouter, Together, Fireworks, Ollama, ...). Without a
    key configured, every user gets a small free trial on the app's own
    Groq/Gemini keys (agents/graph_orchestrator.py's quota check).
    """
    gr.Markdown(
        "## ⚙️ Settings\n"
        "Bring your own LLM key — any OpenAI-compatible provider works. "
        "Without one, you get a free daily trial on the app's shared keys."
    )

    status = gr.Markdown("")

    with gr.Row():
        model_box = gr.Textbox(
            label="Model", placeholder="e.g. gpt-4o-mini, llama-3.3-70b-versatile", scale=2
        )
        base_url_box = gr.Textbox(
            label="Base URL (optional — leave blank for OpenAI itself)",
            placeholder="e.g. https://openrouter.ai/api/v1",
            scale=3,
        )
    api_key_box = gr.Textbox(label="API Key", type="password", placeholder="sk-...")

    with gr.Row():
        save_btn = gr.Button("💾 Save key", variant="primary")
        test_btn = gr.Button("🔌 Test connection", variant="secondary")
        clear_btn = gr.Button("🗑️ Remove my key", variant="secondary")

    def _refresh_status(user_id):
        if not user_id:
            return ""
        return _settings_status_md(user_id)

    def _save(user_id, model, base_url, api_key):
        if not user_id:
            return "", model, base_url, api_key
        # VAL-07: format checks (SEC-01 URL policy included), then a live
        # 1-token call — a key is saved only once the provider has accepted it.
        from utils.errors import AriaError
        from utils.forms import llm_settings, probe_llm

        try:
            model, api_key, base_url = llm_settings(model, api_key, base_url)
            probe_llm(model, api_key, base_url)
        except AriaError as e:
            hint = f" {e.fix_hint}" if e.fix_hint else ""
            return f"⚠️ {e.user_message}{hint}", model or "", base_url or "", api_key or ""
        from utils.crypto import encrypt
        from utils.db import llm_config_set

        try:
            encrypted = encrypt(api_key)
        except ValueError as e:
            log.error(f"[Settings] encrypt() failed — SECRET_KEY misconfigured: {e}")
            return (
                "⚠️ Server misconfiguration: keys can't be saved securely right now. "
                "Please contact the site operator.",
                model,
                base_url or "",
                api_key,
            )
        llm_config_set(user_id, "openai_compatible", model, encrypted, base_url=base_url)
        from utils.audit import audit

        audit(
            "settings.llm_key_set",
            actor_user_id=user_id,
            details={"model": model[:100], "base_url_host": _url_host(base_url)},
        )
        # Evict the cached orchestrator so the new key applies on this
        # user's very next message instead of waiting for a restart/reset —
        # GraphOrchestrator resolves its BYO config once per instance
        # (agents/graph_orchestrator.py), so a stale cached instance would
        # otherwise keep using whatever was configured (or the trial path)
        # when it was first built.
        evict_user(user_id)
        return (
            _settings_status_md(user_id),
            model,
            base_url or "",
            "",
        )  # clear the key field, never echo it back

    def _test(user_id, model, base_url, api_key):
        """One 1-token call with the typed (or, if the key box is empty, the
        saved) config. Reports a coarse error class, never the raw upstream
        text — that would turn this button into a way to read responses from
        arbitrary endpoints (SEC-01)."""
        if not user_id:
            return ""
        from agents.llm_client import _ERROR_CLASS_TEXT, LLMProvidersExhausted, _error_class
        from agents.providers.openai_compatible import OpenAICompatibleProvider
        from utils.url_safety import UnsafeURLError

        model = (model or "").strip()
        base_url = (base_url or "").strip() or None
        api_key = (api_key or "").strip()
        if not api_key:
            from utils.crypto import decrypt
            from utils.db import llm_config_get

            row = llm_config_get(user_id)
            if row is None:
                return "⚠️ Enter a model and API key (or save one) first."
            try:
                api_key = decrypt(row["api_key_encrypted"])
            except ValueError:
                return "⚠️ Your saved key can't be read any more — please enter it again."
            model = model or row["model"]
            base_url = base_url or row["base_url"]
        if not model:
            return "⚠️ Model is required."
        try:
            provider = OpenAICompatibleProvider("Reply with OK.", api_key, model, base_url)
            provider.ask("ping", temperature=0.0, max_tokens=1)
        except UnsafeURLError as e:
            return f"⚠️ {e.user_message}"
        except Exception as e:
            kind = _error_class(LLMProvidersExhausted(e, is_byo=True))
            log.warning(f"[Settings] connection test failed ({kind}): {e}")
            return f"⚠️ Connection test failed: {_ERROR_CLASS_TEXT[kind]}"
        return "✅ Connection works — the provider answered."

    def _clear(user_id):
        if not user_id:
            return "", "", "", ""
        from utils.db import llm_config_clear

        llm_config_clear(user_id)
        from utils.audit import audit

        audit("settings.llm_key_cleared", actor_user_id=user_id)
        evict_user(user_id)
        return _settings_status_md(user_id), "", "", ""

    save_btn.click(
        _save,
        inputs=[user_state, model_box, base_url_box, api_key_box],
        outputs=[status, model_box, base_url_box, api_key_box],
    )
    test_btn.click(_test, inputs=[user_state, model_box, base_url_box, api_key_box], outputs=[status])
    clear_btn.click(_clear, inputs=[user_state], outputs=[status, model_box, base_url_box, api_key_box])
    user_state.change(_refresh_status, inputs=[user_state], outputs=[status])

    gr.Markdown(
        "---\n### 🔔 Telegram Alerts\nConnect your own Telegram chat so alerts you create come straight to you."
    )
    tg_status = gr.Markdown("")

    with gr.Row():
        tg_connect_btn = gr.Button("🔗 Connect Telegram", variant="primary")
        tg_disconnect_btn = gr.Button("🔌 Disconnect", variant="secondary")

    def _telegram_status_md(user_id: int) -> str:
        from utils.db import telegram_chat_id_get

        return (
            "✅ **Telegram connected** — alerts will be sent to your linked chat."
            if telegram_chat_id_get(user_id)
            else "⚪ **Not connected** — click Connect Telegram below to link your chat."
        )

    def _tg_refresh(user_id):
        return _telegram_status_md(user_id) if user_id else ""

    def _tg_connect(user_id):
        if not user_id:
            return ""
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME

        if not TELEGRAM_BOT_TOKEN:
            return "⚠️ This ARIA instance hasn't configured a Telegram bot yet — ask the admin to set `TELEGRAM_BOT_TOKEN`."
        if not TELEGRAM_BOT_USERNAME:
            return "⚠️ Admin needs to set `TELEGRAM_BOT_USERNAME` for the connect link to work."
        from utils.telegram_link import generate_link_code

        code = generate_link_code(user_id)
        link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={code}"
        return (
            f"👉 [**Tap here to connect**]({link}) — opens Telegram, sends `/start` automatically. "
            f"Link expires in 15 minutes."
        )

    def _tg_disconnect(user_id):
        if not user_id:
            return ""
        from utils.db import telegram_chat_id_set

        telegram_chat_id_set(user_id, None)
        from utils.audit import audit

        audit("settings.telegram_unlinked", actor_user_id=user_id)
        return _telegram_status_md(user_id)

    tg_connect_btn.click(_tg_connect, inputs=[user_state], outputs=[tg_status])
    tg_disconnect_btn.click(_tg_disconnect, inputs=[user_state], outputs=[tg_status])
    user_state.change(_tg_refresh, inputs=[user_state], outputs=[tg_status])

    # ── Alert inbox (DATA-08) ────────────────────────────────────────────────
    # Fallback delivery channel: alerts that fired while Telegram wasn't linked
    # (or kept failing) land here instead of being silently dropped.
    gr.Markdown("---\n### 📥 Alert inbox\nAlerts that couldn't be sent to Telegram are kept here.")
    inbox_md = gr.Markdown("")
    with gr.Row():
        inbox_refresh_btn = gr.Button("🔄 Refresh", variant="secondary")
        inbox_read_btn = gr.Button("✓ Mark all read", variant="secondary")

    def _inbox_refresh(user_id, notify: bool = False):
        if not user_id:
            return ""
        from utils.db import notifications_list

        try:
            rows = notifications_list(user_id, limit=30)
        except Exception as e:
            log.warning(f"[Settings] inbox load failed: {e}")
            return "⚠️ Couldn't load your alert inbox right now."
        unread = sum(1 for r in rows if r["read_at"] is None)
        if notify and unread:
            gr.Info(f"📥 You have {unread} unread alert notification(s) — see Settings → Alert inbox.")
        return _inbox_md(rows)

    def _inbox_mark_read(user_id):
        if not user_id:
            return ""
        from utils.db import notifications_mark_read

        notifications_mark_read(user_id)
        return _inbox_refresh(user_id)

    inbox_refresh_btn.click(_inbox_refresh, inputs=[user_state], outputs=[inbox_md])
    inbox_read_btn.click(_inbox_mark_read, inputs=[user_state], outputs=[inbox_md])
    user_state.change(lambda uid: _inbox_refresh(uid, notify=True), inputs=[user_state], outputs=[inbox_md])

    # ── Account security (SEC-04) ─────────────────────────────────────────────
    gr.Markdown("---\n### 🔐 Signed-in devices")
    sessions_md = gr.Markdown("")
    with gr.Row():
        logout_others_btn = gr.Button("Log out other devices", variant="secondary")
        logout_here_btn = gr.Button("Log out", variant="secondary")

    def _sessions(user_id):
        if not user_id:
            return ""
        from utils.db import auth_sessions_list

        try:
            rows = auth_sessions_list(user_id)
        except Exception as e:
            log.warning(f"[Settings] session list failed: {e}")
            return "⚠️ Couldn't load your signed-in devices right now."
        return _sessions_md(rows)

    def _logout_others(user_id, request: gr.Request):
        if not user_id:
            return ""
        from utils.auth import logout_other_devices
        from web.session import COOKIE_NAME

        n = logout_other_devices(user_id, request.cookies.get(COOKIE_NAME))
        from utils.audit import audit

        audit("auth.logout_other_devices", actor_user_id=user_id, details={"revoked": n})
        return f"✅ Logged out {n} other session(s).\n\n" + _sessions(user_id)

    gr.Markdown(
        "**Your data** — [download everything ARIA stores about you](/account/export) (JSON) · "
        "**Delete account** — permanently remove your account and all its data: "
        "[delete my account…](/account/delete)"
    )

    logout_others_btn.click(_logout_others, inputs=[user_state], outputs=[sessions_md])
    logout_here_btn.click(None, js=LOGOUT_JS)
    user_state.change(_sessions, inputs=[user_state], outputs=[sessions_md])

    return status


def _url_host(url: str | None) -> str | None:
    if not url:
        return None
    from urllib.parse import urlsplit

    return urlsplit(url).hostname


def _sessions_md(rows: list[dict]) -> str:
    """Signed-in sessions table; the user agent is escaped (SEC-10)."""
    if not rows:
        return "_No active sessions._"
    lines = ["| Device | Last active (IST) |", "|---|---|"]
    for r in rows:
        ua = md_cell(r.get("user_agent") or "Unknown device", max_len=60)
        seen = (
            r["last_seen_at"].astimezone(IST_TZ).strftime("%d %b %Y, %H:%M") if r.get("last_seen_at") else "—"
        )
        lines.append(f"| {ua} | {seen} |")
    return "\n".join(lines)


def _inbox_md(rows: list[dict]) -> str:
    """Render inbox rows. Alert text is generated by scripts/check_alerts.py
    from symbols/thresholds, but escape it anyway — it's shown as Markdown."""
    import html as _html

    if not rows:
        return "*No alert notifications.*"
    lines = []
    for r in rows:
        when = r["created_at"].astimezone(IST_TZ).strftime("%d %b %H:%M IST") if r.get("created_at") else ""
        dot = "🔵 " if r.get("read_at") is None else ""
        lines.append(f"- {dot}**{when}** — {_html.escape(str(r['message']))}")
    return "\n".join(lines)


_DOWNLOAD_URL_RE = re.compile(r"^/download/(?:report/\d+\.(?:pdf|md)|file/[A-Za-z0-9_-]{16,64})$")


def _download_link(url: str, label: str) -> str:
    """A Markdown link to one of our own download routes (anything else renders nothing)."""
    if not _DOWNLOAD_URL_RE.match(url or ""):
        return ""
    return f"[{label}]({url})"


def _download_links(pdf_url: str | None, md_url: str | None, csv_url: str | None) -> str:
    """OPS-02: the answer's download links, as a line under it ("" if none)."""
    links = [
        _download_link(pdf_url or "", "⬇ Download PDF"),
        _download_link(md_url or "", "⬇ Download Markdown"),
        _download_link(csv_url or "", "⬇ Export results (CSV)"),
    ]
    links = [x for x in links if x]
    return "\n\n" + " · ".join(links) if links else ""


def _request_user(request: gr.Request | None) -> int | None:
    """The user server.py's auth_dependency resolved from the session cookie
    for this request (SEC-04), or None outside the authenticated server."""
    username = getattr(request, "username", None) if request is not None else None
    return int(username) if username and str(username).isdigit() else None


def _identify_user(request: gr.Request) -> int | None:
    """The authenticated user id for this page load (SEC-04)."""
    return _request_user(request)


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="ARIA — Autonomous Financial Research Engine") as demo:
        # SEC-04: who is logged in comes from the session cookie, checked by
        # server.py's auth_dependency on every request (gr.Request.username).
        # user_state is set once per page load from that, never from input.
        user_state = gr.State(None)

        with gr.Column(elem_id="main-app"):
            # Header
            gr.HTML(HEADER_HTML)
            with gr.Row(elem_id="top-actions"):
                theme_btn = gr.Button("◐ Light / dark", size="sm", variant="secondary", elem_id="theme-btn")
                logout_btn = gr.Button("Log out", size="sm", variant="secondary", elem_id="logout-btn")
            theme_btn.click(None, js=THEME_TOGGLE_JS)
            logout_btn.click(None, js=LOGOUT_JS)

            # Tabs — one unified chat (orchestrator-routed) + Watchlist utility view
            with gr.Tabs():
                with gr.Tab("💬 Chat"):
                    chat_chatbot, chat_chart, chat_session, chat_sugg_state = _build_chat_tab(user_state)

                with gr.Tab("📌 Watchlist"):
                    _build_watchlist_tab(user_state)

                with gr.Tab("📊 Portfolio"):
                    _build_portfolio_tab(user_state)

                with gr.Tab("⚙️ Settings"):
                    _build_settings_tab(user_state)

            # Footer
            gr.HTML(FOOTER_HTML)

        # Note: the watchlist table no longer needs a user_state.change
        # handler here — its @gr.render already takes user_state as an
        # input, so it re-renders automatically on login/logout.

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

        demo.load(_identify_user, None, [user_state])

    demo.queue(**_queue_kwargs())
    return demo


# ── Entry point ───────────────────────────────────────────────────────────────

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
    print("  Primary  : Groq (Llama 3.3 70B) — 14,400 req/day per key")
    print("  Fallback : Google Gemini 2.0 Flash")
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
