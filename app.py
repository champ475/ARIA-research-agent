"""
app.py — ARIA v2 Professional Multi-Tab Trading Assistant
Gradio 6 compatible. show_api=False fixes the proxy/launch error.
"""

import os
import gradio as gr
from utils.db    import init_db
from utils.helpers import now_ist
from utils.auth import signup, login, AuthError
from agents.orchestrator import route as orch_route

# ── Init DB on startup ────────────────────────────────────────────────────────
init_db()

# ── Loading indicator HTML (three bouncing dots, styled via global CSS) ──────
LOADING_HTML = (
    '<span class="aria-dot"></span>'
    '<span class="aria-dot"></span>'
    '<span class="aria-dot"></span>'
)

# ── Lazy orchestrator loader ──────────────────────────────────────────────────
# One Orchestrator per user — it owns all four specialist agents internally
# and routes each message to the right one, so the UI only ever talks to a
# single chat surface instead of one tab per agent.
_orchestrators = {}

def get_orchestrator(user_id: int):
    if user_id not in _orchestrators:
        from agents.orchestrator import Orchestrator
        _orchestrators[user_id] = Orchestrator(user_id)
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
}

CHAT_DEFAULTS = [
    "Analyse TCS", "IC report for TCS", "Best ELSS for tax saving", "Top gainers today",
]
CHAT_PLACEHOLDER = (
    "Ask anything — stock analysis, IC/result-update reports, mutual funds, "
    "screening, portfolio, market overview…"
)


def _session_title(msg: str) -> str:
    """First-message-derived title for a new chat session, ChatGPT-style."""
    t = " ".join(msg.strip().split())
    if not t:
        return "New chat"
    return (t[:42] + "…") if len(t) > 42 else t


# ── Core respond function (orchestrator-routed) ───────────────────────────────

def respond(user_id: int, message: str, history: list, suggestions: list, session_id: int, tf: str):
    """
    Generator: first yield shows loading dots, second yield delivers the real
    response. The orchestrator decides which specialist agent handles the
    message; the reply is prefixed with a small badge naming that agent so
    routing stays visible instead of being a hidden implementation detail.
    Yields: (cleared_input, updated_history, chart, suggestions, pdf, md, csv, *btn_updates)
    """
    no_file = gr.update(value=None, visible=False)
    if not message.strip():
        yield ("", history, gr.update(), suggestions, no_file, no_file, no_file) + tuple(gr.update() for _ in range(4))
        return

    clean = message.lstrip("📊📋📈📰🔀🌍❓✅🔍 ").strip()

    # Timeframe keyword only makes sense when equity handles the message —
    # decide the route up front so it isn't injected into an unrelated agent.
    if orch_route(clean) == "equity":
        kw = _TF_KEYWORDS.get(tf, "")
        if kw and kw not in clean.lower():
            clean = clean + " " + kw

    # Immediately show loading indicator while the orchestrator processes
    loading_hist = history + [
        {"role": "user",      "content": message},
        {"role": "assistant", "content": LOADING_HTML},
    ]
    yield ("", loading_hist, gr.update(value=None, visible=False), suggestions, no_file, no_file, no_file) + _make_btn_updates(suggestions)

    chart_fig  = None
    new_sugg   = suggestions
    pdf_update = no_file
    md_update  = no_file
    csv_update = no_file
    text       = ""
    try:
        orch = get_orchestrator(user_id)
        text, chart, pdf_path, md_path, csv_path, tab_used = orch.process(clean)
        if chart is not None:
            chart_fig = chart
        new_sugg = orch.generate_suggestions(text, clean)
        text = f"*🔀 Routed to: {_TAB_LABELS.get(tab_used, tab_used)}*\n\n" + text
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
        text = f"⚠️ Error: `{str(e)}`\n\nPlease try again."

    _persist_turn(user_id, session_id, message, text, is_first=not history)

    updated_history = history + [
        {"role": "user",      "content": message},
        {"role": "assistant", "content": text},
    ]
    chart_update = gr.update(value=chart_fig, visible=chart_fig is not None)
    btn_updates  = _make_btn_updates(new_sugg)
    yield ("", updated_history, chart_update, new_sugg, pdf_update, md_update, csv_update) + btn_updates


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
    Left sidebar listing this user's past chat sessions. Clicking a session
    loads its saved messages into the chatbot and resumes every specialist
    agent's conversation memory (the orchestrator doesn't know in advance
    which one(s) an old conversation touched). "+ New chat" detaches from
    the current session without deleting it, so the next message starts a
    fresh thread. `extra_reset` is any extra output components (download
    buttons etc.) that should hide when switching sessions. Returns the
    sidebar's Column so the caller can wire a collapse/expand toggle.
    """
    from utils.db import session_list, session_delete, chat_load

    extra_reset = extra_reset or []
    load_outputs = [session_state, chatbot, chart_panel, suggestions_state] + sug_btns + extra_reset

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

    with gr.Column(scale=1, min_width=230, elem_classes=["chat-sidebar"]) as sidebar_col:
        gr.Markdown("**Chats**", elem_classes=["sidebar-title"])
        new_chat_btn = gr.Button("＋ New chat", size="sm", elem_classes=["new-chat-btn"])
        new_chat_btn.click(_new_chat, inputs=[user_state], outputs=load_outputs)

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

    return sidebar_col


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
/* ChatGPT-style chat shell: centered column, sticky toolbar */
.chat-shell { gap: 0 !important; }
.chat-main {
    max-width: 820px !important;
    margin: 0 auto !important;
}
.chat-toolbar {
    align-items: center !important;
    gap: 8px !important;
    padding: 4px 0 8px 0 !important;
    border-bottom: 1px solid #21262d !important;
    margin-bottom: 8px !important;
}
.chat-toolbar-title { flex: 1; }
.sidebar-toggle-btn {
    background: #161b22 !important;
    color: #8b949e !important;
    border: 1px solid #30363d !important;
    border-radius: 8px !important;
    font-size: 14px !important;
    padding: 4px 10px !important;
}
.sidebar-toggle-btn:hover { color: #58a6ff !important; border-color: #58a6ff66 !important; }
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
    sidebar_open    = gr.State(True)
    suggestions_state = gr.State(CHAT_DEFAULTS)

    with gr.Row(elem_classes=["chat-shell"]):
        with gr.Column(scale=4, elem_classes=["chat-main"]):
            with gr.Row(elem_classes=["chat-toolbar"]):
                sidebar_toggle = gr.Button("☰", size="sm", elem_classes=["sidebar-toggle-btn"], min_width=36)
                gr.Markdown("**💬 ARIA Chat**", elem_classes=["chat-toolbar-title"])

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
                clear_btn = gr.Button("Clear",   scale=1, min_width=60, elem_classes=["clear-btn"])

        sidebar_col = _build_chat_sidebar(tab_id, user_state, session_state, refresh_state,
                                           chatbot, chart_panel, suggestions_state, sug_btns,
                                           extra_reset=[pdf_btn, md_btn, csv_btn])

    def _toggle_sidebar(is_open):
        new_state = not is_open
        return new_state, gr.update(visible=new_state)

    sidebar_toggle.click(_toggle_sidebar, [sidebar_open], [sidebar_open, sidebar_col])

    outputs = ([msg_box, chatbot, chart_panel, suggestions_state, pdf_btn, md_btn, csv_btn]
               + sug_btns + [session_state, refresh_state])

    def _respond(user_id, session_id, refresh, msg, hist, sugg, tf):
        if session_id is None and msg.strip():
            from utils.db import session_create
            session_id = session_create(user_id, tab_id, title=_session_title(msg))
        for i, out in enumerate(respond(user_id, msg, hist, sugg, session_id, tf)):
            yield out + (session_id, refresh if i == 0 else refresh + 1)

    def _clear(user_id):
        hist, chart, sugg, *btns = clear_chat(user_id)
        no_dl = gr.update(value=None, visible=False)
        return [hist, chart, sugg, no_dl, no_dl, no_dl] + list(btns) + [None]

    msg_box.submit(_respond,  [user_state, session_state, refresh_state, msg_box, chatbot, suggestions_state, tf_radio], outputs)
    send_btn.click(_respond,  [user_state, session_state, refresh_state, msg_box, chatbot, suggestions_state, tf_radio], outputs)
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

            # RSI from cached OHLCV (fast — usually already in Parquet cache)
            rsi = "—"
            try:
                df = get_ohlcv(sym, "3mo", "1d")
                if not df.empty:
                    ta = snapshot(compute_all(df.copy()))
                    rsi = ta.get("RSI", "—")
            except Exception:
                pass

            arrow = "▲" if chg >= 0 else "▼"
            rows.append([sym, name, f"₹{price:,.2f}", f"{arrow} {chg:+.2f}%", rsi,
                         f"₹{high52:,.2f}" if isinstance(high52, float) else str(high52),
                         f"₹{low52:,.2f}"  if isinstance(low52,  float) else str(low52)])
        except Exception:
            rows.append([sym, "—", "—", "—", "—", "—", "—"])
    return rows


def _build_watchlist_tab(user_state: gr.State):
    """Watchlist tab: add/remove symbols, live price table with RSI, refresh button."""
    from utils.db import watchlist_add, watchlist_remove
    from utils.helpers import clean_ticker

    COLS = ["Symbol", "Name", "Price", "Change %", "RSI", "52W High", "52W Low"]

    gr.Markdown("## 📌 Watchlist\nTrack live prices and key indicators for your saved symbols.")

    # Price table (populated on login via app.load, since it needs user_id)
    wl_table = gr.Dataframe(
        value=[],
        headers=COLS,
        datatype=["str"] * len(COLS),
        interactive=False,
        elem_id="wl-table",
    )

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

    def _add(user_id, sym_raw):
        sym = clean_ticker(sym_raw.strip().upper())
        if not sym:
            return _fetch_watchlist_table(user_id), "⚠️ Enter a valid NSE symbol."
        watchlist_add(user_id, sym)
        return _fetch_watchlist_table(user_id), f"✅ **{sym}** added to watchlist."

    def _remove(user_id, sym_raw):
        sym = clean_ticker(sym_raw.strip().upper())
        if not sym:
            return _fetch_watchlist_table(user_id), "⚠️ Enter a symbol to remove."
        watchlist_remove(user_id, sym)
        return _fetch_watchlist_table(user_id), f"🗑️ **{sym}** removed from watchlist."

    def _refresh(user_id):
        rows = _fetch_watchlist_table(user_id)
        n = len(rows)
        return rows, f"🔄 Refreshed {n} symbol{'s' if n != 1 else ''}."

    add_btn.click(_add,     inputs=[user_state, add_box],  outputs=[wl_table, status])
    rem_btn.click(_remove,  inputs=[user_state, rem_box],  outputs=[wl_table, status])
    ref_btn.click(_refresh, inputs=[user_state],           outputs=[wl_table, status])
    add_box.submit(_add,    inputs=[user_state, add_box],  outputs=[wl_table, status])

    return wl_table, status


# ── Auth gate ─────────────────────────────────────────────────────────────────

def _build_auth_gate():
    """
    Login/signup screen. On success, sets user_state to the authenticated
    user_id, hides this gate, and reveals the main app.
    Returns (auth_group, main_group, user_state).
    """
    user_state = gr.State(None)  # holds user_id once authenticated

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
            auth_msg  = gr.Markdown("")
            with gr.Row():
                login_btn  = gr.Button("Log in", variant="primary")
                signup_btn = gr.Button("Sign up", variant="secondary")

    with gr.Column(elem_id="main-app", visible=False) as main_group:
        pass  # populated by caller

    def _do_login(email, password):
        try:
            token = login(email, password)
        except AuthError as e:
            return str(e), gr.update(visible=True), gr.update(visible=False), None
        from utils.auth import get_user_id
        uid = get_user_id(token)
        return "", gr.update(visible=False), gr.update(visible=True), uid

    def _do_signup(email, password):
        try:
            signup(email, password)
            token = login(email, password)
        except AuthError as e:
            return str(e), gr.update(visible=True), gr.update(visible=False), None
        from utils.auth import get_user_id
        uid = get_user_id(token)
        return "", gr.update(visible=False), gr.update(visible=True), uid

    login_btn.click(_do_login,  [email_box, pass_box], [auth_msg, auth_group, main_group, user_state])
    signup_btn.click(_do_signup, [email_box, pass_box], [auth_msg, auth_group, main_group, user_state])
    pass_box.submit(_do_login,  [email_box, pass_box], [auth_msg, auth_group, main_group, user_state])

    return auth_group, main_group, user_state


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="ARIA — Professional Investment Research") as demo:

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
                Groq Llama 3.3 70B → Gemini 2.0 Flash fallback
              </div>
            </div>
            """)

            # Tabs — one unified chat (orchestrator-routed) + Watchlist utility view
            with gr.Tabs():
                with gr.Tab("💬 Chat"):
                    chat_chatbot, chat_chart, chat_session, chat_sugg_state = _build_chat_tab(user_state)

                with gr.Tab("📌 Watchlist") as watchlist_tab:
                    wl_table, wl_status = _build_watchlist_tab(user_state)

            # Footer
            gr.HTML("""
            <div style='text-align:center;font-size:10px;color:#484f58;padding:6px 0 10px'>
              Data: NSE/BSE via yfinance · NSE bhavcopy · AMFI · Google News RSS — All free &nbsp;|&nbsp;
              ⚠️ Not SEBI-registered. For professional research purposes only.
            </div>
            """)

        # Populate the watchlist table once a user is authenticated.
        def _load_watchlist(user_id):
            if user_id is None:
                return [], ""
            return _fetch_watchlist_table(user_id), ""

        user_state.change(_load_watchlist, [user_state], [wl_table, wl_status])

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
