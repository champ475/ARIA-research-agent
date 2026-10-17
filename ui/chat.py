"""
ui/chat.py — the Chat tab: the conversation, its sidebar of past chats, the
Yes/No row for a paused research step, feedback and the first-run card.

This module renders and wires Gradio events. What a turn needs from the rest of
ARIA (the orchestrator, rate limit, saving, sessions, feedback) comes from
services/chat.py; this file imports neither the database nor the data tools.
"""

from __future__ import annotations

import logging
import time

import gradio as gr

from config import CHAT_CONCURRENCY, MAX_CHAT_CHARS, QUEUE_MAX_SIZE, TURNS_PER_MINUTE
from graph.progress import ProgressUpdate
from services import chat as chat_svc
from ui.auth import request_user
from utils import lifecycle
from utils.errors import AriaError, ConfigurationError, UserInputError, error_id_from_turn, render_error
from utils.escaping import html_text
from utils.input_guard import CleanMessage, InputRejected, clean_message
from utils.logging_setup import run_in_turn
from utils.url_safety import UnsafeURLError

log = logging.getLogger(__name__)

# ── Loading indicator HTML (three bouncing dots, styled via global CSS) ──────
LOADING_HTML = '<span class="aria-dot"></span><span class="aria-dot"></span><span class="aria-dot"></span>'


def queue_kwargs() -> dict:
    return {"default_concurrency_limit": CHAT_CONCURRENCY, "max_size": QUEUE_MAX_SIZE}


def progress_html(steps: list[str]) -> str:
    """Loading dots plus the last few research steps (UX-02)."""
    shown = steps[-4:]
    trail = " → ".join(html_text(s) for s in shown[:-1])
    current = html_text(shown[-1]) + "…" if shown else ""
    return (
        f"{LOADING_HTML}<div class='aria-progress' role='status' aria-live='polite'>"
        f"{trail + ' → ' if trail else ''}<strong>{current}</strong></div>"
    )


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

    clean = chat_svc.with_timeframe(user_id, session_id, clean, tf)

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
    ) + make_btn_updates(suggestions)

    chart_fig = None
    new_sugg = suggestions
    text = ""
    tab_used: str | None = None
    try:
        orch = chat_svc.get_orchestrator(user_id, session_id)
        tab_used = "equity"
        # Equity Q&A streams token-by-token; every other specialist yields
        # exactly once with its finished result, so this loop renders
        # identically for both — it just runs once for a non-streaming tab.
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
                        {"role": "assistant", "content": progress_html(steps)},
                    ],
                    gr.update(),
                    suggestions,
                ) + make_btn_updates(suggestions)
                continue
            text, chart, pdf_url, md_url, csv_url, tab_used = out
            if chart is not None:
                chart_fig = chart
            streaming_hist = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": chat_svc.routing_badge(tab_used) + text},
            ]
            yield (
                _INPUT_BUSY,
                _SEND_BUSY,
                streaming_hist,
                gr.update(value=chart_fig, visible=chart_fig is not None),
                suggestions,
            ) + make_btn_updates(suggestions)

        new_sugg = orch.generate_suggestions(text, clean)
        text = chat_svc.routing_badge(tab_used) + text
        # OPS-02: links to /download/... routes, which any replica can serve.
        # Links live in the answer itself: a separate component's update raced
        # the sidebar re-render and was often lost (TEST-04), and in the
        # message they stay usable from the chat history.
        text += chat_svc.download_links(pdf_url, md_url, csv_url)
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
    # what re-enables the input box. Losing one turn's chat-history row is a
    # much smaller problem than leaving the whole input permanently locked.
    try:
        chat_svc.persist_turn(user_id, session_id, message, text, is_first=not history, route=tab_used)
    except Exception as e:
        log.warning(f"[respond] failed to persist turn for session {session_id}: {e}")

    updated_history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": text},
    ]
    chart_update = gr.update(value=chart_fig, visible=chart_fig is not None)
    btn_updates = make_btn_updates(new_sugg)
    log.info(f"[respond] turn end ms={int((time.monotonic() - started) * 1000)}")
    yield (
        _INPUT_READY,
        _SEND_READY,
        updated_history,
        chart_update,
        new_sugg,
    ) + btn_updates


def respond_exclusive(user_id, message, history, suggestions, session_id, tf):
    """respond(), but at most one turn per conversation at a time (REL-01), with
    every log line of the turn tagged by run_in_turn (OBS-01)."""
    if lifecycle.is_draining():
        # OPS-04: this instance is shutting down; don't start work it would
        # have to cut off. The message survives in the box for a resend.
        log.info("[respond] rejected: server draining for shutdown")
        yield busy_outputs(message, history, suggestions, text=_DRAINING_TEXT)
        return
    if not chat_svc.chat_enabled():
        log.info("[respond] rejected: chat switched off by the operator")
        yield busy_outputs(message, history, suggestions, text=_PAUSED_TEXT)
        return
    if not chat_svc.turn_allowed(user_id):
        log.info("[respond] rejected: per-user turn rate limit")
        yield busy_outputs(message, history, suggestions, text=_RATE_LIMITED_TEXT)
        return
    lock = chat_svc.user_turn_lock(user_id, session_id)
    if not lock.acquire(blocking=False):
        log.info("[respond] rejected: user already has a turn in flight")
        yield busy_outputs(message, history, suggestions)
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
_PAUSED_TEXT = "🛠 ARIA is paused for maintenance. Please try again shortly."
_RATE_LIMITED_TEXT = (
    f"🐢 You're sending messages faster than ARIA can answer them (limit: {TURNS_PER_MINUTE} a minute). "
    "Please wait a moment and send this again."
)


def busy_outputs(message, history: list, suggestions: list, text: str = _BUSY_TEXT) -> tuple:
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


def clear_chat(user_id: int):
    # "Clear" starts a new chat; the old one stays saved with its own state.
    return ([], gr.update(value=None, visible=False), CHAT_DEFAULTS) + make_btn_updates(CHAT_DEFAULTS)


def make_btn_updates(sugg: list) -> tuple:
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
    extra_reset = extra_reset or []
    load_outputs = [session_state, chatbot, chart_panel, suggestions_state] + sug_btns + extra_reset
    sidebar_open = gr.State(True)
    search_state = gr.State("")
    shown_state = gr.State(chat_svc.SESSION_PAGE)  # how many chats the list shows (paging)

    def _blank_update():
        core = [None, [], gr.update(value=None, visible=False), CHAT_DEFAULTS] + list(
            make_btn_updates(CHAT_DEFAULTS)
        )
        extras = [gr.update(value=None, visible=False) for _ in extra_reset]
        return tuple(core + extras)

    def _new_chat(user_id):
        # A new chat gets its own session (and orchestrator) on its first message.
        return _blank_update()

    def _load_session(user_id, sid):
        hist = chat_svc.load_session(user_id, sid)
        core = [sid, hist, gr.update(value=None, visible=False), CHAT_DEFAULTS] + list(
            make_btn_updates(CHAT_DEFAULTS)
        )
        extras = [gr.update(value=None, visible=False) for _ in extra_reset]
        return tuple(core + extras)

    def _delete_session(user_id, sid, current_sid, refresh):
        chat_svc.delete_session(user_id, sid)
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
            search_box = gr.Textbox(
                placeholder="Search chats (Enter)",
                show_label=False,
                label="Search chats",
                max_lines=1,
                max_length=80,
                elem_id="chat-search",
            )
            gr.Markdown("**Chats**", elem_classes=["sidebar-title"])

            # session_state is in the render inputs so the active chat gets a
            # highlighted row and the list re-renders the moment it changes.
            @gr.render(inputs=[user_state, refresh_state, session_state, search_state, shown_state])
            def _render_sessions(user_id, _refresh, active_sid, query, shown):
                if not user_id:
                    return
                # one extra row says whether there is another page
                found = (
                    chat_svc.search_sessions(user_id, tab_id, query, limit=shown + 1)
                    if query
                    else chat_svc.list_sessions(user_id, tab_id, limit=shown + 1)
                )
                sessions, more = found[:shown], len(found) > shown
                if not sessions:
                    gr.Markdown(
                        "*No chats match that*" if query else "*No past chats yet*",
                        elem_classes=["sidebar-empty"],
                    )
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
                if more:
                    more_btn = gr.Button("Show more", size="sm", elem_id="chat-more")
                    more_btn.click(
                        lambda n: n + chat_svc.SESSION_PAGE,
                        [shown_state],
                        [shown_state],
                        show_progress="hidden",
                    )

            with gr.Accordion("This chat", open=False, elem_id="chat-manage"):
                rename_box = gr.Textbox(
                    show_label=False, label="Chat name", placeholder="New name", max_lines=1, max_length=80,
                    elem_id="chat-rename",
                )  # fmt: skip
                rename_btn = gr.Button("Rename", size="sm", elem_id="chat-rename-btn")
                export_btn = gr.Button("Export as Markdown", size="sm", elem_id="chat-export-btn")
                manage_note = gr.Markdown("", elem_id="chat-manage-note")

    def _search(text):
        return (text or "").strip(), chat_svc.SESSION_PAGE

    def _rename(user_id, sid, title, refresh, request: gr.Request):
        user_id = request_user(request) or user_id
        if not user_id or sid is None:
            return refresh, "Open a chat first."
        try:
            new = chat_svc.rename_session(user_id, sid, title)
        except UserInputError as e:
            return refresh, f"⚠️ {html_text(e.user_message)}"
        except LookupError:
            return refresh, "⚠️ That chat no longer exists."
        return refresh + 1, f"Renamed to “{html_text(new)}”."

    def _export(user_id, sid, request: gr.Request):
        user_id = request_user(request) or user_id
        if not user_id or sid is None:
            return "Open a chat first."
        link = chat_svc.export_chat(user_id, sid)
        return f"[⬇ Download this chat (Markdown)]({link})" if link else "Nothing to export yet."

    search_box.submit(_search, [search_box], [search_state, shown_state], show_progress="hidden")
    rename_btn.click(
        _rename, [user_state, session_state, rename_box, refresh_state], [refresh_state, manage_note]
    )
    export_btn.click(_export, [user_state, session_state], [manage_note])

    new_chat_btn.click(_new_chat, inputs=[user_state], outputs=load_outputs, show_progress="hidden")
    new_chat_icon_btn.click(_new_chat, inputs=[user_state], outputs=load_outputs, show_progress="hidden")

    def _toggle(is_open):
        new_state = not is_open
        rail_class = ["chat-sidebar"] if new_state else ["chat-sidebar", "collapsed"]
        return new_state, gr.update(visible=new_state), gr.update(elem_classes=rail_class)

    toggle_btn.click(_toggle, [sidebar_open], [sidebar_open, expanded_panel, sidebar_col])

    return sidebar_col, sidebar_open, toggle_btn


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


def build_chat_tab(user_state: gr.State):
    """
    One chat surface for everything — equity research, report generation,
    mutual funds, screening. The orchestrator decides which specialist handles
    each message; the user never picks a tab for it.
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
        user_id = request_user(request) or user_id
        if not user_id:
            return
        if session_id is None and isinstance(clean_message(msg), CleanMessage):
            session_id = chat_svc.create_session(user_id, tab_id, msg)
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
        user_id = request_user(request) or user_id
        return gr.update(visible=chat_svc.confirmation_pending(user_id, session_id))

    def _on_like(data: gr.LikeData, user_id, session_id, request: gr.Request):
        """Thumbs up/down on an answer, linked to the turn that produced it."""
        user_id = request_user(request) or user_id
        hidden = (gr.update(visible=False), None, "")
        if not user_id or session_id is None:
            return hidden
        index = data.index[0] if isinstance(data.index, (list, tuple)) else data.index
        outcome, turn_id = chat_svc.record_like(user_id, session_id, index, data.liked)
        if outcome == "up":
            return gr.update(visible=False), None, "👍 Thanks for the feedback."
        if outcome == "down":
            return gr.update(visible=True), turn_id, ""
        return hidden

    def _send_feedback(user_id, session_id, turn_id, reason, details, request: gr.Request):
        user_id = request_user(request) or user_id
        if user_id and turn_id:
            chat_svc.send_feedback(user_id, session_id, turn_id, reason, details)
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
        user_id = request_user(request) or user_id
        if not user_id:
            return gr.update(visible=False), ""
        info = chat_svc.onboarding_info(user_id)
        if info is None:
            return gr.update(visible=False), ""
        return gr.update(visible=True), onboarding_card(info)

    def _onboarding_dismiss(user_id, request: gr.Request):
        user_id = request_user(request) or user_id
        if user_id:
            chat_svc.dismiss_onboarding(user_id)
        return gr.update(visible=False)

    # Load-time events hide Gradio's progress: its pending fade could stay on
    # the target after a slow first load (TEST-07), see _reset_chat in build_ui.
    user_state.change(_onboarding_view, [user_state], [onboarding_box, onboarding_md], show_progress="hidden")
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
