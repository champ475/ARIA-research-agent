"""
app.py — Gradio front end for ARIA, an autonomous financial research engine.
Gradio 6 compatible. show_api=False fixes the proxy/launch error.
"""

import os
import time
import logging
import gradio as gr
from utils.logging_setup import configure_logging, run_in_turn

# Before anything else logs: JSON lines with turn ids + secret redaction (OBS-01).
configure_logging()

from utils.db    import init_db
from utils.helpers import now_ist
from utils.auth import signup, login, AuthError
from utils.telegram_link import start_polling_thread

log = logging.getLogger(__name__)

# ── Init DB on startup ────────────────────────────────────────────────────────
init_db()

# ── Start the Telegram /start-code listener (no-ops if no bot token set) ──────
start_polling_thread()

# ── Loading indicator HTML (three bouncing dots, styled via global CSS) ──────
LOADING_HTML = (
    '<span class="aria-dot"></span>'
    '<span class="aria-dot"></span>'
    '<span class="aria-dot"></span>'
)

# ── Lazy orchestrator loader ──────────────────────────────────────────────────
# One GraphOrchestrator per user (agents/graph_orchestrator.py) — the LangGraph
# research-graph engine, see RFC_AGENTIC_RESEARCH_GRAPH.md, is the only
# orchestration path now (the legacy keyword-routed Orchestrator + its 6
# specialist agent classes were retired once the graph reached feature parity
# across all 6 domains — see ARCHITECTURE.md).
_orchestrators = {}

def get_orchestrator(user_id: int):
    if user_id not in _orchestrators:
        from agents.graph_orchestrator import GraphOrchestrator
        _orchestrators[user_id] = GraphOrchestrator(user_id)
    return _orchestrators[user_id]


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
    "equity"  : "📊 Equity Research",
    "report"  : "📋 Report Generator",
    "mf"      : "📈 Mutual Funds",
    "screener": "🔍 Screener",
    "alert"   : "🔔 Alerts",
    "planning": "🧭 Financial Planner",
}


def _tab_label(tab_used: str) -> str:
    """Renders a routing badge label for `tab_used`, including a joined
    multi-domain label (e.g. "report+alert" -> "📋 Report Generator + 🔔
    Alerts") when the graph orchestrator names more than one domain for this
    turn (Phase 7.4 multi-intent support) — falls back to the raw segment
    for any domain name not in _TAB_LABELS."""
    parts = tab_used.split("+")
    return " + ".join(_TAB_LABELS.get(p, p) for p in parts)

CHAT_DEFAULTS = [
    "Analyse TCS", "IC report for TCS", "Best ELSS for tax saving", "Top gainers today",
]
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

_INPUT_BUSY  = gr.update(value="", interactive=False)   # locked while a turn is in flight
_INPUT_READY = gr.update(value="", interactive=True)     # unlocked once the turn is done
_SEND_BUSY   = gr.update(interactive=False)
_SEND_READY  = gr.update(interactive=True)


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

    Yields: (cleared_input, send_btn, updated_history, chart, suggestions, pdf, md, csv, *btn_updates)
    """
    no_file = gr.update(value=None, visible=False)
    if not message.strip():
        yield (_INPUT_READY, _SEND_READY, history, gr.update(), suggestions, no_file, no_file, no_file) + tuple(gr.update() for _ in range(4))
        return

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
        if get_orchestrator(user_id).classify(clean) == "equity":
            kw = _TF_KEYWORDS.get(tf, "")
            if kw and kw not in clean.lower():
                clean = clean + " " + kw
    except Exception:
        pass

    # Immediately show loading indicator while the orchestrator processes
    loading_hist = history + [
        {"role": "user",      "content": message},
        {"role": "assistant", "content": LOADING_HTML},
    ]
    yield (_INPUT_BUSY, _SEND_BUSY, loading_hist, gr.update(value=None, visible=False), suggestions, no_file, no_file, no_file) + _make_btn_updates(suggestions)

    chart_fig  = None
    new_sugg   = suggestions
    pdf_update = no_file
    md_update  = no_file
    csv_update = no_file
    text       = ""
    try:
        orch = get_orchestrator(user_id)
        tab_used = "equity"
        # Equity Q&A streams token-by-token (see EquityAgent.process_stream);
        # every other specialist yields exactly once with its finished
        # result, so this loop renders identically for both — it just runs
        # once for a non-streaming tab.
        for text, chart, pdf_path, md_path, csv_path, tab_used in orch.process_stream(clean):
            if chart is not None:
                chart_fig = chart
            streaming_hist = history + [
                {"role": "user",      "content": message},
                {"role": "assistant", "content": f"*🔀 Routed to: {_tab_label(tab_used)}*\n\n" + text},
            ]
            yield (
                _INPUT_BUSY, _SEND_BUSY, streaming_hist, gr.update(value=chart_fig, visible=chart_fig is not None),
                suggestions, no_file, no_file, no_file,
            ) + _make_btn_updates(suggestions)

        new_sugg = orch.generate_suggestions(text, clean)
        text = f"*🔀 Routed to: {_tab_label(tab_used)}*\n\n" + text
        if pdf_path:
            pdf_update = gr.update(value=pdf_path, label=f"⬇ {os.path.basename(pdf_path)}", visible=True)
        if md_path:
            md_update = gr.update(value=md_path,  label=f"⬇ {os.path.basename(md_path)}",  visible=True)
        if csv_path:
            csv_update = gr.update(value=csv_path, label="⬇ Export Results CSV", visible=True)
    except ValueError as e:
        text = (
            f"### ⚠️ API Key Required\n\n{e}\n\n"
            "Add to `.env`:\n"
            "```\nGROQ_API_KEY_1=your_key   # free at console.groq.com\n"
            "GEMINI_API_KEY=your_key    # free at aistudio.google.com/apikey\n```\n"
            "Then restart: `python app.py`"
        )
    except Exception as e:
        log.exception("[respond] turn failed")
        text = f"⚠️ Error: `{str(e)}`\n\nPlease try again."

    # A save failure here (e.g. a Neon connection dropped for being idle —
    # utils/db.py now retries once, but a second failure is still possible)
    # must never stop the terminal yield below from firing: that yield is
    # what re-enables the input box (_INPUT_READY/_SEND_READY, the
    # Finding-1 fix). Losing one turn's chat-history row is a much smaller
    # problem than leaving the whole input permanently locked.
    try:
        _persist_turn(user_id, session_id, message, text, is_first=not history)
    except Exception as e:
        log.warning(f"[respond] failed to persist turn for session {session_id}: {e}")

    updated_history = history + [
        {"role": "user",      "content": message},
        {"role": "assistant", "content": text},
    ]
    chart_update = gr.update(value=chart_fig, visible=chart_fig is not None)
    btn_updates  = _make_btn_updates(new_sugg)
    log.info(f"[respond] turn end ms={int((time.monotonic() - started) * 1000)}")
    yield (_INPUT_READY, _SEND_READY, updated_history, chart_update, new_sugg, pdf_update, md_update, csv_update) + btn_updates


def _persist_turn(user_id: int, session_id: int, user_msg: str, assistant_msg: str, is_first: bool):
    from utils.db import chat_save, session_touch
    chat_save(user_id, session_id, "chat", "user", user_msg)
    chat_save(user_id, session_id, "chat", "assistant", assistant_msg)
    session_touch(session_id, title=_session_title(user_msg) if is_first else None)


def clear_chat(user_id: int):
    if user_id in _orchestrators:
        _orchestrators[user_id].reset()
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

def _build_chat_sidebar(tab_id, user_state, session_state, refresh_state,
                         chatbot, chart_panel, suggestions_state, sug_btns,
                         extra_reset=None):
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
    from utils.db import session_list, session_delete, chat_load

    extra_reset = extra_reset or []
    load_outputs = [session_state, chatbot, chart_panel, suggestions_state] + sug_btns + extra_reset
    sidebar_open = gr.State(True)

    def _blank_update():
        core = [None, [], gr.update(value=None, visible=False), CHAT_DEFAULTS] + list(_make_btn_updates(CHAT_DEFAULTS))
        extras = [gr.update(value=None, visible=False) for _ in extra_reset]
        return tuple(core + extras)

    def _new_chat(user_id):
        if user_id:
            get_orchestrator(user_id).reset()
        return _blank_update()

    def _load_session(user_id, sid):
        msgs = chat_load(user_id, sid)
        hist = [{"role": m["role"], "content": m["content"]} for m in msgs]
        get_orchestrator(user_id).load_history(hist)
        core = [sid, hist, gr.update(value=None, visible=False), CHAT_DEFAULTS] + list(_make_btn_updates(CHAT_DEFAULTS))
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
            new_chat_icon_btn = gr.Button("✎", size="sm", elem_classes=["sidebar-icon-btn", "collapsed-only"], min_width=32)

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
                        inputs=[], outputs=load_outputs,
                    )
                    d.click(
                        fn=lambda current_sid, refresh, uid=user_id, s=sid: _delete_session(uid, s, current_sid, refresh),
                        inputs=[session_state, refresh_state],
                        outputs=load_outputs + [refresh_state],
                    )

    new_chat_btn.click(_new_chat, inputs=[user_state], outputs=load_outputs)
    new_chat_icon_btn.click(_new_chat, inputs=[user_state], outputs=load_outputs)

    def _toggle(is_open):
        new_state = not is_open
        rail_class = ["chat-sidebar"] if new_state else ["chat-sidebar", "collapsed"]
        return new_state, gr.update(visible=new_state), gr.update(elem_classes=rail_class)

    toggle_btn.click(_toggle, [sidebar_open], [sidebar_open, expanded_panel, sidebar_col])

    return sidebar_col, sidebar_open, toggle_btn


# ── CSS ───────────────────────────────────────────────────────────────────────
CSS = """
body, .gradio-container {
    background: #0d1117 !important;
    color: #e6edf3 !important;
}
.gradio-container {
    max-width: 100% !important;
    padding: 0 !important;
    margin: 0 !important;
}
/* Tabs */
.tab-nav button {
    background: #161b22 !important;
    color: #8b949e !important;
    border: none !important;
    border-bottom: 2px solid transparent !important;
    padding: 10px 20px !important;
    font-size: 13px !important;
}
.tab-nav button.selected {
    color: #58a6ff !important;
    border-bottom: 2px solid #58a6ff !important;
    background: #0d1117 !important;
}
/* Chatbot */
.chatbot {
    background: #0d1117 !important;
    border: none !important;
}
/* Message bubbles */
.message.user > div {
    background: #1c2128 !important;
    border: 1px solid #30363d !important;
    border-radius: 12px 12px 4px 12px !important;
    color: #e6edf3 !important;
}
.message.bot > div {
    background: transparent !important;
    color: #e6edf3 !important;
}
.message.bot table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    background: #161b22;
    border-radius: 6px;
    overflow: hidden;
}
.message.bot th {
    background: #21262d;
    color: #58a6ff;
    padding: 7px 12px;
    border-bottom: 1px solid #30363d;
}
.message.bot td { padding: 6px 12px; border-bottom: 1px solid #21262d; }
.message.bot code { background: #1f2937; color: #79c0ff; padding: 1px 6px; border-radius: 4px; }
/* Input */
.input-row textarea {
    background: #161b22 !important;
    border: 1px solid #30363d !important;
    color: #e6edf3 !important;
    border-radius: 10px !important;
}
.input-row textarea:focus { border-color: #58a6ff !important; outline: none !important; }
/* Suggestion chips */
.sugg-btn {
    background: #161b22 !important;
    color: #8b949e !important;
    border: 1px solid #30363d !important;
    border-radius: 16px !important;
    font-size: 12px !important;
    padding: 4px 12px !important;
}
.sugg-btn:hover {
    color: #58a6ff !important;
    border-color: #58a6ff66 !important;
}
/* Send / Clear buttons */
.send-btn { background: #1f6feb !important; color: white !important; border-radius: 8px !important; border: none !important; }
.send-btn:hover { background: #388bfd !important; }
.clear-btn { background: transparent !important; color: #6e7681 !important; border: 1px solid #30363d !important; border-radius: 8px !important; }
.clear-btn:hover { color: #f85149 !important; border-color: #f85149 !important; }
/* Top header */
#aria-header {
    background: #161b22;
    border-bottom: 1px solid #30363d;
    padding: 8px 20px;
}
footer { display: none !important; }
/* Typing / loading indicator */
@keyframes aria-bounce {
    0%, 80%, 100% { transform: translateY(0);    opacity: 0.35; }
    40%            { transform: translateY(-5px); opacity: 1;    }
}
.aria-dot {
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 50%;
    background: #58a6ff;
    margin: 0 2px;
    vertical-align: middle;
    animation: aria-bounce 1.2s infinite ease-in-out;
}
.aria-dot:nth-child(2) { animation-delay: 0.2s; }
.aria-dot:nth-child(3) { animation-delay: 0.4s; }
/* Download buttons */
.dl-btn {
    flex: 0 0 auto !important;
    width: auto !important;
    min-width: 0 !important;
}
.dl-btn button {
    border-radius: 20px !important;
    font-size: 12px !important;
    padding: 5px 18px !important;
    font-weight: 500 !important;
    letter-spacing: 0.2px !important;
    white-space: nowrap !important;
}
.dl-row {
    gap: 8px !important;
    padding: 6px 0 2px 0 !important;
    justify-content: flex-start !important;
    flex-wrap: wrap !important;
}
/* Timeframe radio */
.tf-radio label { font-size: 12px !important; color: #8b949e !important; }
.tf-radio .wrap { gap: 4px !important; }
/* Chat sidebar (ChatGPT-style) */
.chat-sidebar {
    background: #0a0d12 !important;
    border-right: 1px solid #30363d !important;
    padding: 8px !important;
    max-height: 640px;
    overflow-y: auto;
}
.sidebar-title { color: #8b949e !important; font-size: 11px !important; padding: 2px 4px !important; }
.new-chat-btn {
    background: #161b22 !important;
    color: #e6edf3 !important;
    border: 1px solid #30363d !important;
    border-radius: 8px !important;
    font-size: 12px !important;
    margin-bottom: 6px !important;
    width: 100% !important;
}
.new-chat-btn:hover { border-color: #58a6ff66 !important; }
.sidebar-empty { color: #6e7681 !important; font-size: 11px !important; padding: 6px 4px !important; }
.session-row { gap: 4px !important; margin-bottom: 2px !important; }
.session-btn {
    background: transparent !important;
    color: #c9d1d9 !important;
    border: none !important;
    text-align: left !important;
    justify-content: flex-start !important;
    font-size: 12px !important;
    padding: 6px 8px !important;
    border-radius: 6px !important;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
}
.session-btn:hover { background: #161b22 !important; color: #58a6ff !important; }
.session-btn-active {
    background: #161b22 !important;
    color: #58a6ff !important;
    border-left: 2px solid #58a6ff !important;
    font-weight: 600 !important;
}
.session-del-btn {
    background: transparent !important;
    color: #6e7681 !important;
    border: none !important;
    font-size: 11px !important;
    padding: 4px !important;
}
.session-del-btn:hover { color: #f85149 !important; }
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
    background: #0a0d12 !important;
    border-right: 1px solid #30363d !important;
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
    color: #c9d1d9 !important;
    border: 1px solid transparent !important;
    border-radius: 8px !important;
    font-size: 15px !important;
    padding: 6px !important;
    width: 32px !important;
    min-width: 32px !important;
    max-width: 32px !important;
    flex: 0 0 auto !important;
}
.sidebar-icon-btn:hover { background: #161b22 !important; border-color: #30363d !important; }
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
    background: #161b22;
    border-radius: 6px;
    overflow: hidden;
}
.wl-table th {
    background: #21262d;
    color: #58a6ff;
    padding: 8px 12px;
    text-align: left;
    border-bottom: 1px solid #30363d;
}
.wl-table td {
    padding: 7px 12px;
    border-bottom: 1px solid #21262d;
    color: #e6edf3;
}
.wl-table tr:last-child td { border-bottom: none; }
.wl-empty { color: #6e7681; font-size: 13px; padding: 10px 2px; }
"""


# ── Build the single unified chat tab (orchestrator-routed) ──────────────────

def _build_chat_tab(user_state: gr.State):
    """
    One chat surface for everything — equity research, report generation,
    mutual funds, screening. The orchestrator (agents/orchestrator.py)
    decides which specialist agent handles each message; the user never
    picks a tab for it.
    """
    tab_id = "chat"

    session_state   = gr.State(None)
    refresh_state   = gr.State(0)
    suggestions_state = gr.State(CHAT_DEFAULTS)

    # Sidebar has to be *built* after chatbot/chart_panel/sug_btns exist (it
    # wires click handlers that target them), but must *render* on the left
    # like ChatGPT — solved with `order: -1` in CSS (.chat-sidebar) rather
    # than fighting component build order.
    with gr.Row(elem_classes=["chat-shell"]):
        with gr.Column(scale=4, elem_classes=["chat-main"]):
            chatbot = gr.Chatbot(
                label="", show_label=False, height=480,
                render_markdown=True, elem_id=f"chatbot-{tab_id}",
            )
            chart_panel = gr.Plot(value=None, elem_id=f"chart-{tab_id}", show_label=False, visible=False)

            # Timeframe selector — only applies when the orchestrator routes to equity
            with gr.Row(elem_classes=["tf-radio"]):
                tf_radio = gr.Radio(
                    choices=["1D", "1W", "1M", "3M", "6M", "2Y"],
                    value="1D",
                    label="Chart timeframe (equity queries)",
                    interactive=True,
                    scale=1,
                )

            # Download buttons — report PDF/MD and screener CSV, hidden until produced
            with gr.Row(elem_classes=["dl-row"]):
                pdf_btn = gr.DownloadButton("⬇ Download PDF",      value=None, visible=False,
                                            variant="primary",   size="sm", elem_classes=["dl-btn"])
                md_btn  = gr.DownloadButton("⬇ Download Markdown", value=None, visible=False,
                                            variant="secondary", size="sm", elem_classes=["dl-btn"])
                csv_btn = gr.DownloadButton("⬇ Export Results CSV", value=None, visible=False,
                                            variant="secondary", size="sm", elem_classes=["dl-btn"])

            gr.Markdown("**Suggested:**", elem_id=f"sugg-label-{tab_id}")
            with gr.Row():
                sug_btns = [
                    gr.Button(CHAT_DEFAULTS[i], size="sm", elem_classes=["sugg-btn"])
                    for i in range(4)
                ]

            with gr.Row(elem_id=f"input-row-{tab_id}"):
                msg_box  = gr.Textbox(
                    placeholder=CHAT_PLACEHOLDER, label="", show_label=False,
                    lines=1, max_lines=3, autofocus=True,
                    elem_id=f"msg-{tab_id}", scale=10,
                )
                send_btn  = gr.Button("Send ⚡", variant="primary", scale=1, min_width=80, elem_classes=["send-btn"])
                clear_btn = gr.Button("Clear chat", scale=1, min_width=60, elem_classes=["clear-btn"])

        _build_chat_sidebar(tab_id, user_state, session_state, refresh_state,
                             chatbot, chart_panel, suggestions_state, sug_btns,
                             extra_reset=[pdf_btn, md_btn, csv_btn])

    outputs = ([msg_box, send_btn, chatbot, chart_panel, suggestions_state, pdf_btn, md_btn, csv_btn]
               + sug_btns + [session_state, refresh_state])

    def _respond(user_id, session_id, refresh, msg, hist, sugg, tf):
        if session_id is None and msg.strip():
            from utils.db import session_create
            session_id = session_create(user_id, tab_id, title=_session_title(msg))
            # A freshly created session must show up in the sidebar the moment
            # this turn starts, not on some later yield — bump refresh_state
            # right away instead of leaving it to the loop below (see
            # ARCHITECTURE.md: the sidebar's @gr.render previously missed a
            # brand-new session for the rest of that browser session).
            refresh += 1
        # run_in_turn keeps one turn_id on every log line of this turn, across
        # yields (Gradio resumes each generator step in a fresh context).
        turn = run_in_turn(respond(user_id, msg, hist, sugg, session_id, tf), user_id, session_id)
        for i, out in enumerate(turn):
            yield out + (session_id, refresh if i == 0 else refresh + 1)

    def _clear(user_id):
        hist, chart, sugg, *btns = clear_chat(user_id)
        no_dl = gr.update(value=None, visible=False)
        return [hist, chart, sugg, no_dl, no_dl, no_dl] + list(btns) + [None]

    # show_progress="hidden" on both: Gradio's own generic ETA overlay
    # ("processing | Xs/Ys" text + a stray link icon) otherwise renders on
    # top of the chat area and even inside the input box once a turn runs
    # long — most visibly under LLM-provider rate-limiting, exactly when the
    # app most needs to look composed. respond()'s own LOADING_HTML
    # (bouncing dots, prefixed to the assistant bubble) is the one loading
    # indicator meant to be visible; this stops Gradio's from fighting it.
    msg_box.submit(_respond,  [user_state, session_state, refresh_state, msg_box, chatbot, suggestions_state, tf_radio], outputs, show_progress="hidden")
    send_btn.click(_respond,  [user_state, session_state, refresh_state, msg_box, chatbot, suggestions_state, tf_radio], outputs, show_progress="hidden")
    clear_btn.click(
        _clear, [user_state],
        [chatbot, chart_panel, suggestions_state, pdf_btn, md_btn, csv_btn] + sug_btns + [session_state],
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
    from utils.db import watchlist_all
    from tools.data.fetcher import get_info, get_ohlcv
    from tools.analysis.technical import compute_all, snapshot

    symbols = watchlist_all(user_id)
    if not symbols:
        return []

    rows = []
    for sym in symbols:
        try:
            info  = get_info(sym)
            price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
            prev  = info.get("previousClose") or price
            chg   = round(((price - prev) / prev) * 100, 2) if prev else 0.0
            name  = (info.get("shortName") or sym)[:22]
            high52 = info.get("fiftyTwoWeekHigh", "N/A")
            low52  = info.get("fiftyTwoWeekLow",  "N/A")

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
            rows.append([sym, name, f"₹{price:,.2f}", f"{arrow} {chg:+.2f}%", rsi,
                         f"₹{high52:,.2f}" if isinstance(high52, float) else str(high52),
                         f"₹{low52:,.2f}"  if isinstance(low52,  float) else str(low52)])
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
    head = "".join(f"<th>{c}</th>" for c in WATCHLIST_COLS)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
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
        add_box  = gr.Textbox(placeholder="Add symbol (e.g. TCS, RELIANCE)", label="", show_label=False,
                              scale=4, lines=1, max_lines=1)
        add_btn  = gr.Button("➕ Add",     variant="primary",   scale=1, min_width=80)
        rem_box  = gr.Textbox(placeholder="Remove symbol",      label="", show_label=False,
                              scale=4, lines=1, max_lines=1)
        rem_btn  = gr.Button("➖ Remove",  variant="secondary", scale=1, min_width=80)
        ref_btn  = gr.Button("🔄 Refresh", variant="secondary", scale=1, min_width=90)

    def _add(user_id, sym_raw, refresh):
        sym = clean_ticker(sym_raw.strip().upper())
        if not sym:
            return refresh, "⚠️ Enter a valid NSE symbol."
        watchlist_add(user_id, sym)
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

    add_btn.click(_add,          inputs=[user_state, add_box, wl_refresh],  outputs=[wl_refresh, status])
    rem_btn.click(_remove,       inputs=[user_state, rem_box, wl_refresh],  outputs=[wl_refresh, status])
    ref_btn.click(_refresh_click, inputs=[user_state, wl_refresh],          outputs=[wl_refresh, status])
    add_box.submit(_add,         inputs=[user_state, add_box, wl_refresh], outputs=[wl_refresh, status])

    return status


# ── Settings tab (bring-your-own LLM key) ──────────────────────────────────────

_TRIAL_DAILY_LIMIT = 5


def _settings_status_md(user_id: int) -> str:
    from utils.db import llm_config_get, _query
    row = llm_config_get(user_id)
    if row:
        base = row["base_url"] or "api.openai.com (or provider default)"
        return f"✅ **Using your own key** — model `{row['model']}` via `{base}`."
    urow = _query(
        "SELECT free_msgs_used_today, free_msgs_reset_at FROM users WHERE id=%s",
        (user_id,), fetch="one",
    )
    used = urow["free_msgs_used_today"] if urow else 0
    from datetime import datetime
    from utils.helpers import IST
    today = datetime.now(IST).date()
    if urow and urow["free_msgs_reset_at"] != today:
        used = 0
    remaining = max(0, _TRIAL_DAILY_LIMIT - used)
    return f"🆓 **Free trial** — {remaining}/{_TRIAL_DAILY_LIMIT} messages left today. Add your own key below for unlimited use."


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
        model_box = gr.Textbox(label="Model", placeholder="e.g. gpt-4o-mini, llama-3.3-70b-versatile", scale=2)
        base_url_box = gr.Textbox(label="Base URL (optional — leave blank for OpenAI itself)",
                                   placeholder="e.g. https://openrouter.ai/api/v1", scale=3)
    api_key_box = gr.Textbox(label="API Key", type="password", placeholder="sk-...")

    with gr.Row():
        save_btn = gr.Button("💾 Save key", variant="primary")
        clear_btn = gr.Button("🗑️ Remove my key", variant="secondary")

    def _refresh_status(user_id):
        if not user_id:
            return ""
        return _settings_status_md(user_id)

    def _save(user_id, model, base_url, api_key):
        if not user_id:
            return "", model, base_url, api_key
        model = (model or "").strip()
        api_key = (api_key or "").strip()
        base_url = (base_url or "").strip() or None
        if not model or not api_key:
            return "⚠️ Model and API key are both required.", model, base_url or "", api_key
        from utils.crypto import encrypt
        from utils.db import llm_config_set
        try:
            encrypted = encrypt(api_key)
        except ValueError as e:
            log.error(f"[Settings] encrypt() failed — SECRET_KEY misconfigured: {e}")
            return (
                "⚠️ Server misconfiguration: keys can't be saved securely right now. "
                "Please contact the site operator.",
                model, base_url or "", api_key,
            )
        llm_config_set(user_id, "openai_compatible", model, encrypted, base_url=base_url)
        # Evict the cached orchestrator so the new key applies on this
        # user's very next message instead of waiting for a restart/reset —
        # GraphOrchestrator resolves its BYO config once per instance
        # (agents/graph_orchestrator.py), so a stale cached instance would
        # otherwise keep using whatever was configured (or the trial path)
        # when it was first built.
        _orchestrators.pop(user_id, None)
        return _settings_status_md(user_id), model, base_url or "", ""  # clear the key field, never echo it back

    def _clear(user_id):
        if not user_id:
            return "", "", "", ""
        from utils.db import llm_config_clear
        llm_config_clear(user_id)
        _orchestrators.pop(user_id, None)
        return _settings_status_md(user_id), "", "", ""

    save_btn.click(_save, inputs=[user_state, model_box, base_url_box, api_key_box],
                    outputs=[status, model_box, base_url_box, api_key_box])
    clear_btn.click(_clear, inputs=[user_state],
                     outputs=[status, model_box, base_url_box, api_key_box])
    user_state.change(_refresh_status, inputs=[user_state], outputs=[status])

    gr.Markdown("---\n### 🔔 Telegram Alerts\nConnect your own Telegram chat so alerts you create come straight to you.")
    tg_status = gr.Markdown("")

    with gr.Row():
        tg_connect_btn = gr.Button("🔗 Connect Telegram", variant="primary")
        tg_disconnect_btn = gr.Button("🔌 Disconnect", variant="secondary")

    def _telegram_status_md(user_id: int) -> str:
        from utils.db import telegram_chat_id_get
        return "✅ **Telegram connected** — alerts will be sent to your linked chat." \
            if telegram_chat_id_get(user_id) else \
            "⚪ **Not connected** — click Connect Telegram below to link your chat."

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
        return _telegram_status_md(user_id)

    tg_connect_btn.click(_tg_connect, inputs=[user_state], outputs=[tg_status])
    tg_disconnect_btn.click(_tg_disconnect, inputs=[user_state], outputs=[tg_status])
    user_state.change(_tg_refresh, inputs=[user_state], outputs=[tg_status])

    return status


# ── Auth gate ─────────────────────────────────────────────────────────────────

def _build_auth_gate():
    """
    Login/signup screen. On success, sets user_state to the authenticated
    user_id, hides this gate, and reveals the main app.
    Returns (auth_group, main_group, user_state).
    """
    user_state = gr.State(None)  # holds user_id once authenticated

    def _load_legal_text() -> str:
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LEGAL.md")
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return "Terms of Use & Privacy Notice unavailable — contact this instance's operator."

    with gr.Column(elem_id="auth-gate", visible=True) as auth_group:
        gr.HTML(
            "<div style='text-align:center;padding:30px 0 10px'>"
            "<span style='font-size:22px;font-weight:700;color:#58a6ff;'>⚡ ARIA</span><br/>"
            "<span style='font-size:12px;color:#8b949e;'>Sign in to your research workspace</span>"
            "</div>"
        )
        with gr.Column(scale=1, min_width=360, elem_id="auth-card"):
            email_box = gr.Textbox(label="Email", placeholder="you@example.com")
            pass_box  = gr.Textbox(label="Password", type="password", placeholder="At least 8 characters")
            with gr.Accordion("📜 Terms of Use & Privacy Notice", open=False):
                gr.Markdown(_load_legal_text(), elem_id="legal-text")
            terms_box = gr.Checkbox(label="I agree to the Terms of Use & Privacy Notice above", value=False)
            auth_msg  = gr.Markdown("")
            with gr.Row():
                login_btn  = gr.Button("Log in", variant="primary")
                signup_btn = gr.Button("Sign up", variant="secondary")

    with gr.Column(elem_id="main-app", visible=False) as main_group:
        pass  # populated by caller

    def _do_login(email, password, _agreed):
        try:
            token = login(email, password)
        except AuthError as e:
            return str(e), gr.update(visible=True), gr.update(visible=False), None
        from utils.auth import get_user_id
        uid = get_user_id(token)
        return "", gr.update(visible=False), gr.update(visible=True), uid

    def _do_signup(email, password, agreed):
        if not agreed:
            return "⚠️ Please agree to the Terms of Use & Privacy Notice to sign up.", gr.update(visible=True), gr.update(visible=False), None
        try:
            uid = signup(email, password)
            from utils.db import accept_terms
            accept_terms(uid)
            token = login(email, password)
        except AuthError as e:
            return str(e), gr.update(visible=True), gr.update(visible=False), None
        from utils.auth import get_user_id
        uid = get_user_id(token)
        return "", gr.update(visible=False), gr.update(visible=True), uid

    login_btn.click(_do_login,  [email_box, pass_box, terms_box], [auth_msg, auth_group, main_group, user_state])
    signup_btn.click(_do_signup, [email_box, pass_box, terms_box], [auth_msg, auth_group, main_group, user_state])
    pass_box.submit(_do_login,  [email_box, pass_box, terms_box], [auth_msg, auth_group, main_group, user_state])

    return auth_group, main_group, user_state


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="ARIA — Autonomous Financial Research Engine") as demo:

        auth_group, main_group, user_state = _build_auth_gate()

        with main_group:
            # Header
            gr.HTML("""
            <div id='aria-header' style='display:flex;align-items:center;justify-content:space-between'>
              <div>
                <span style='font-size:18px;font-weight:700;color:#58a6ff;'>⚡ ARIA</span>
                <span style='font-size:11px;color:#8b949e;margin-left:10px;'>
                  Autonomous Research &amp; Investment Analyst · NSE · BSE · v2
                </span>
              </div>
              <div style='font-size:10px;padding:3px 10px;border-radius:12px;
                          background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb44;'>
                Free trial · bring your own key for unlimited use (Settings)
              </div>
            </div>
            """)

            # Tabs — one unified chat (orchestrator-routed) + Watchlist utility view
            with gr.Tabs():
                with gr.Tab("💬 Chat"):
                    chat_chatbot, chat_chart, chat_session, chat_sugg_state = _build_chat_tab(user_state)

                with gr.Tab("📌 Watchlist") as watchlist_tab:
                    _build_watchlist_tab(user_state)

                with gr.Tab("⚙️ Settings"):
                    _build_settings_tab(user_state)

            # Footer
            gr.HTML("""
            <div style='text-align:center;font-size:10px;color:#484f58;padding:6px 0 10px'>
              Data: NSE/BSE via yfinance · NSE bhavcopy · AMFI · Google News RSS — All free &nbsp;|&nbsp;
              ⚠️ Not SEBI-registered. For professional research purposes only.
            </div>
            """)

        # Note: the watchlist table no longer needs a user_state.change
        # handler here — its @gr.render already takes user_state as an
        # input, so it re-renders automatically on login/logout.

        # Reset the visible chat + active session on login/logout, so a
        # different account signing in on the same browser tab never sees the
        # previous user's conversation (UI-level tenant isolation, not just DB).
        def _reset_chat(user_id):
            return [], gr.update(value=None, visible=False), CHAT_DEFAULTS, None

        user_state.change(
            _reset_chat, [user_state],
            [chat_chatbot, chat_chart, chat_sugg_state, chat_session],
        )

    return demo


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    print("=" * 62)
    print("  ⚡ ARIA v2 — Professional Investment Research Platform")
    print("  NSE · BSE · Mutual Funds · Screener")
    print("=" * 62)
    print(f"  Started  : {now_ist()}")
    print("  URL      : http://localhost:7860")
    print("  Primary  : Groq (Llama 3.3 70B) — 14,400 req/day per key")
    print("  Fallback : Google Gemini 2.0 Flash")
    print("=" * 62)
    from config import SECRET_KEY
    if not SECRET_KEY:
        print("  ⚠️  SECRET_KEY is not set — BYO LLM key encryption (Settings tab)")
        print("      will fail. Add SECRET_KEY=<any random string> to .env.")
        print("=" * 62)

    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,0.0.0.0")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1,0.0.0.0")

    demo = build_ui()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
        quiet=False,
        theme=gr.themes.Base(),
        css=CSS,
        # Force Gradio's built-in dark theme vars regardless of the visitor's
        # OS/browser color-scheme preference — this app has one deliberate
        # dark aesthetic, not a light/dark toggle. Without this, users on a
        # light-mode OS get Gradio's default light component backgrounds
        # (white Chatbot/Radio/Dataframe boxes) clashing with our dark CSS,
        # since Gradio only applies its dark theme vars when `.dark` is on
        # <body> (not <html>), which it otherwise sets from
        # prefers-color-scheme. Must be an IIFE — Gradio evaluates `js` as a
        # bare expression, it does not call a bare arrow function for you.
        js="(() => { document.body.classList.add('dark'); })()",
    )
