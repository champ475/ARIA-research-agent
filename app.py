"""
app.py — ARIA v2 Professional Multi-Tab Trading Assistant
Gradio 6 compatible. show_api=False fixes the proxy/launch error.
"""

import os
import gradio as gr
from utils.db    import init_db
from utils.helpers import now_ist

# ── Init DB on startup ────────────────────────────────────────────────────────
init_db()

# ── Loading indicator HTML (three bouncing dots, styled via global CSS) ──────
LOADING_HTML = (
    '<span class="aria-dot"></span>'
    '<span class="aria-dot"></span>'
    '<span class="aria-dot"></span>'
)

# ── Lazy agent loader ─────────────────────────────────────────────────────────
_agents = {}

def get_agent(tab: str):
    if tab not in _agents:
        if tab == "equity":
            from agents.equity_agent   import EquityAgent;   _agents[tab] = EquityAgent()
        elif tab == "report":
            from agents.report_agent   import ReportAgent;   _agents[tab] = ReportAgent()
        elif tab == "mf":
            from agents.mf_agent       import MFAgent;       _agents[tab] = MFAgent()
        elif tab == "screener":
            from agents.screener_agent import ScreenerAgent; _agents[tab] = ScreenerAgent()
    return _agents[tab]


# ── Timeframe keyword map (appended to message when non-default) ──────────────
_TF_KEYWORDS = {
    "1D": "",
    "1W": "weekly",
    "1M": "monthly",
    "3M": "3mo",
    "6M": "6mo",
    "2Y": "2y",
}

# ── Default suggestions per tab ───────────────────────────────────────────────
TAB_DEFAULTS = {
    "equity" : ["Analyse TCS", "Chart RELIANCE weekly", "Compare INFY vs WIPRO", "Market overview today"],
    "report" : ["IC report for TCS", "Result update Reliance", "IT sector thematic", "Quick note HDFCBANK"],
    "mf"     : ["Compare large cap funds", "Best ELSS for tax saving", "SIP vs lumpsum advice", "Mid cap fund options"],
    "screener": ["Quality factor screen", "IT stocks PE below 25", "Top gainers today", "Bulk deals analysis"],
}

TAB_PLACEHOLDERS = {
    "equity"  : "e.g. 'analyse TCS' · 'chart RELIANCE weekly' · 'backtest RSI INFY' · 'compare INFY vs WIPRO'",
    "report"  : "e.g. 'IC report for TCS' · 'result update Reliance' · 'IT sector thematic report'",
    "mf"      : "e.g. 'compare large cap funds' · 'best ELSS for tax saving' · 'SIP advice for 10 years'",
    "screener": "e.g. 'IT stocks PE below 25 ROE above 15' · 'quality factor screen' · 'top gainers today'",
}


# ── Core respond function ─────────────────────────────────────────────────────

def respond(tab: str, message: str, history: list, suggestions: list):
    """
    Generator: first yield shows loading dots, second yield delivers the real response.
    Yields: (cleared_input, updated_history, chart_update, new_suggestions, *btn_updates)
    """
    if not message.strip():
        yield ("", history, gr.update(), suggestions) + tuple(gr.update() for _ in range(4))
        return

    clean = message.lstrip("📊📋📈📰🔀🌍❓✅🔍 ").strip()

    # Immediately show loading indicator while agent processes
    loading_hist = history + [
        {"role": "user",      "content": message},
        {"role": "assistant", "content": LOADING_HTML},
    ]
    yield ("", loading_hist, gr.update(value=None, visible=False), suggestions) + _make_btn_updates(suggestions)

    chart_fig = None
    new_sugg  = suggestions
    text      = ""
    try:
        agent       = get_agent(tab)
        text, chart = agent.process(clean)
        if chart is not None:
            chart_fig = chart
        new_sugg = agent.generate_suggestions(text, clean)
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

    updated_history = history + [
        {"role": "user",      "content": message},
        {"role": "assistant", "content": text},
    ]
    chart_update = gr.update(value=chart_fig, visible=chart_fig is not None)
    btn_updates  = _make_btn_updates(new_sugg)
    yield ("", updated_history, chart_update, new_sugg) + btn_updates


def respond_report(message: str, history: list, suggestions: list):
    """
    Generator: first yield shows loading dots, second yield delivers the real response.
    Like respond() but handles the report agent's 4-tuple output and populates
    gr.DownloadButton components.
    Yields: (cleared_input, updated_history, chart, suggestions, pdf_btn, md_btn, *btn_updates)
    """
    no_file = gr.update(value=None, visible=False)
    if not message.strip():
        yield ("", history, gr.update(), suggestions, no_file, no_file) + tuple(gr.update() for _ in range(4))
        return

    clean = message.lstrip("📊📋📈📰🔀🌍❓✅🔍 ").strip()

    # Immediately show loading indicator while agent processes
    loading_hist = history + [
        {"role": "user",      "content": message},
        {"role": "assistant", "content": LOADING_HTML},
    ]
    yield ("", loading_hist, gr.update(value=None, visible=False), suggestions, no_file, no_file) + _make_btn_updates(suggestions)

    chart_fig  = None
    new_sugg   = suggestions
    pdf_update = no_file
    md_update  = no_file
    text       = ""
    try:
        agent = get_agent("report")
        text, chart, pdf_path, md_path = agent.process(clean)
        if chart is not None:
            chart_fig = chart
        new_sugg = agent.generate_suggestions(text, clean)
        if pdf_path:
            pdf_update = gr.update(value=pdf_path, label=f"⬇ {os.path.basename(pdf_path)}", visible=True)
        if md_path:
            md_update = gr.update(value=md_path,  label=f"⬇ {os.path.basename(md_path)}",  visible=True)
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

    updated_history = history + [
        {"role": "user",      "content": message},
        {"role": "assistant", "content": text},
    ]
    chart_update = gr.update(value=chart_fig, visible=chart_fig is not None)
    btn_updates  = _make_btn_updates(new_sugg)
    yield ("", updated_history, chart_update, new_sugg, pdf_update, md_update) + btn_updates


def clear_tab(tab: str):
    if tab in _agents:
        _agents[tab].reset()
    sugg = TAB_DEFAULTS[tab]
    return ([], gr.update(value=None, visible=False), sugg) + _make_btn_updates(sugg)



def _make_btn_updates(sugg: list) -> tuple:
    updates = []
    for i in range(4):
        if i < len(sugg):
            updates.append(gr.update(value=sugg[i], visible=True))
        else:
            updates.append(gr.update(value="", visible=False))
    return tuple(updates)


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
"""


# ── Build tab UI (reused for mf tab) ─────────────────────────────────────────

def _build_tab(tab_id: str):
    defaults = TAB_DEFAULTS[tab_id]
    placeholder = TAB_PLACEHOLDERS[tab_id]

    history_state    = gr.State([])
    suggestions_state = gr.State(defaults)

    # Chat window
    chatbot = gr.Chatbot(
        label="",
        show_label=False,
        height=480,
        render_markdown=True,
        elem_id=f"chatbot-{tab_id}",
    )

    # Chart panel
    chart_panel = gr.Plot(value=None, elem_id=f"chart-{tab_id}", show_label=False, visible=False)

    # Suggestion chips
    gr.Markdown("**Suggested:**", elem_id=f"sugg-label-{tab_id}")
    with gr.Row():
        sug_btns = [
            gr.Button(defaults[i], size="sm", elem_classes=["sugg-btn"])
            for i in range(4)
        ]

    # Input row
    with gr.Row(elem_id=f"input-row-{tab_id}"):
        msg_box  = gr.Textbox(
            placeholder=placeholder, label="", show_label=False,
            lines=1, max_lines=3, autofocus=(tab_id == "equity"),
            elem_id=f"msg-{tab_id}", scale=10,
        )
        send_btn  = gr.Button("Send ⚡", variant="primary", scale=1, min_width=80, elem_classes=["send-btn"])
        clear_btn = gr.Button("Clear", scale=1, min_width=60, elem_classes=["clear-btn"])

    # Outputs list
    outputs = [msg_box, chatbot, chart_panel, suggestions_state] + sug_btns

    # Respond handler
    def _respond(msg, hist, sugg):
        yield from respond(tab_id, msg, hist, sugg)

    def _clear():
        result = clear_tab(tab_id)
        # result = ([], no_chart, sugg) + btn_updates
        hist, chart, sugg = result[0], result[1], result[2]
        btns = result[3:]
        return [hist, chart, sugg] + list(btns)

    # Wire events
    msg_box.submit(_respond,  [msg_box, chatbot, suggestions_state], outputs)
    send_btn.click(_respond,  [msg_box, chatbot, suggestions_state], outputs)
    clear_btn.click(
        _clear, [],
        [chatbot, chart_panel, suggestions_state] + sug_btns,
    )

    # Suggestion button clicks: populate the input box only, do not submit.
    for i, btn in enumerate(sug_btns):
        btn.click(
            fn=lambda btn_val: (btn_val or "").strip(),
            inputs=[btn],
            outputs=[msg_box],
        )

    return chatbot, chart_panel, sug_btns, msg_box


def _build_equity_tab():
    """Equity tab: standard layout + timeframe selector radio."""
    tab_id      = "equity"
    defaults    = TAB_DEFAULTS[tab_id]
    placeholder = TAB_PLACEHOLDERS[tab_id]

    suggestions_state = gr.State(defaults)

    chatbot = gr.Chatbot(
        label="", show_label=False, height=480,
        render_markdown=True, elem_id=f"chatbot-{tab_id}",
    )
    chart_panel = gr.Plot(value=None, elem_id=f"chart-{tab_id}", show_label=False, visible=False)

    # Timeframe selector — appends a keyword to the message so the agent's _timeframe() picks it up
    with gr.Row(elem_classes=["tf-radio"]):
        tf_radio = gr.Radio(
            choices=["1D", "1W", "1M", "3M", "6M", "2Y"],
            value="1D",
            label="Chart timeframe",
            interactive=True,
            scale=1,
        )

    gr.Markdown("**Suggested:**", elem_id=f"sugg-label-{tab_id}")
    with gr.Row():
        sug_btns = [
            gr.Button(defaults[i], size="sm", elem_classes=["sugg-btn"])
            for i in range(4)
        ]

    with gr.Row(elem_id=f"input-row-{tab_id}"):
        msg_box  = gr.Textbox(
            placeholder=placeholder, label="", show_label=False,
            lines=1, max_lines=3, autofocus=True,
            elem_id=f"msg-{tab_id}", scale=10,
        )
        send_btn  = gr.Button("Send ⚡", variant="primary", scale=1, min_width=80, elem_classes=["send-btn"])
        clear_btn = gr.Button("Clear",   scale=1, min_width=60, elem_classes=["clear-btn"])

    outputs = [msg_box, chatbot, chart_panel, suggestions_state] + sug_btns

    def _respond(msg, hist, sugg, tf):
        # Append timeframe keyword so agent's _timeframe() can parse it
        kw = _TF_KEYWORDS.get(tf, "")
        if kw and kw not in msg.lower():
            msg = msg.strip() + " " + kw
        yield from respond(tab_id, msg, hist, sugg)

    def _clear():
        result = clear_tab(tab_id)
        hist, chart, sugg = result[0], result[1], result[2]
        btns = result[3:]
        return [hist, chart, sugg] + list(btns)

    msg_box.submit(_respond,  [msg_box, chatbot, suggestions_state, tf_radio], outputs)
    send_btn.click(_respond,  [msg_box, chatbot, suggestions_state, tf_radio], outputs)
    clear_btn.click(
        _clear, [],
        [chatbot, chart_panel, suggestions_state] + sug_btns,
    )

    for btn in sug_btns:
        btn.click(
            fn=lambda btn_val: (btn_val or "").strip(),
            inputs=[btn],
            outputs=[msg_box],
        )


def _build_report_tab():
    """Report tab: identical to _build_tab but adds PDF + Markdown download components."""
    tab_id      = "report"
    defaults    = TAB_DEFAULTS[tab_id]
    placeholder = TAB_PLACEHOLDERS[tab_id]

    suggestions_state = gr.State(defaults)

    chatbot = gr.Chatbot(
        label="", show_label=False, height=480,
        render_markdown=True, elem_id=f"chatbot-{tab_id}",
    )
    chart_panel = gr.Plot(value=None, elem_id=f"chart-{tab_id}", show_label=False, visible=False)

    # Download buttons — appear directly below the response, hidden until report is ready
    with gr.Row(elem_classes=["dl-row"]):
        pdf_btn = gr.DownloadButton("⬇ Download PDF",      value=None, visible=False,
                                    variant="primary",   size="sm", elem_classes=["dl-btn"])
        md_btn  = gr.DownloadButton("⬇ Download Markdown", value=None, visible=False,
                                    variant="secondary", size="sm", elem_classes=["dl-btn"])

    gr.Markdown("**Suggested:**", elem_id=f"sugg-label-{tab_id}")
    with gr.Row():
        sug_btns = [
            gr.Button(defaults[i], size="sm", elem_classes=["sugg-btn"])
            for i in range(4)
        ]

    with gr.Row(elem_id=f"input-row-{tab_id}"):
        msg_box  = gr.Textbox(
            placeholder=placeholder, label="", show_label=False,
            lines=1, max_lines=3, autofocus=False,
            elem_id=f"msg-{tab_id}", scale=10,
        )
        send_btn  = gr.Button("Send ⚡", variant="primary", scale=1, min_width=80, elem_classes=["send-btn"])
        clear_btn = gr.Button("Clear",   scale=1, min_width=60, elem_classes=["clear-btn"])

    outputs = [msg_box, chatbot, chart_panel, suggestions_state, pdf_btn, md_btn] + sug_btns

    def _respond(msg, hist, sugg):
        yield from respond_report(msg, hist, sugg)

    def _clear():
        result = clear_tab(tab_id)
        hist, chart, sugg = result[0], result[1], result[2]
        btns = result[3:]
        no_dl = gr.update(value=None, visible=False)
        return [hist, chart, sugg, no_dl, no_dl] + list(btns)

    msg_box.submit(_respond,  [msg_box, chatbot, suggestions_state], outputs)
    send_btn.click(_respond,  [msg_box, chatbot, suggestions_state], outputs)
    clear_btn.click(
        _clear, [],
        [chatbot, chart_panel, suggestions_state, pdf_btn, md_btn] + sug_btns,
    )

    for btn in sug_btns:
        btn.click(
            fn=lambda btn_val: (btn_val or "").strip(),
            inputs=[btn],
            outputs=[msg_box],
        )


def _build_screener_tab():
    """Screener tab: standard layout + CSV export download button."""
    tab_id      = "screener"
    defaults    = TAB_DEFAULTS[tab_id]
    placeholder = TAB_PLACEHOLDERS[tab_id]

    suggestions_state = gr.State(defaults)

    chatbot = gr.Chatbot(
        label="", show_label=False, height=480,
        render_markdown=True, elem_id=f"chatbot-{tab_id}",
    )
    chart_panel = gr.Plot(value=None, elem_id=f"chart-{tab_id}", show_label=False, visible=False)

    # CSV download — appears after a screen is run
    with gr.Row(elem_classes=["dl-row"]):
        csv_btn = gr.DownloadButton(
            "⬇ Export Results CSV", value=None, visible=False,
            variant="secondary", size="sm", elem_classes=["dl-btn"],
        )

    gr.Markdown("**Suggested:**", elem_id=f"sugg-label-{tab_id}")
    with gr.Row():
        sug_btns = [
            gr.Button(defaults[i], size="sm", elem_classes=["sugg-btn"])
            for i in range(4)
        ]

    with gr.Row(elem_id=f"input-row-{tab_id}"):
        msg_box  = gr.Textbox(
            placeholder=placeholder, label="", show_label=False,
            lines=1, max_lines=3, autofocus=False,
            elem_id=f"msg-{tab_id}", scale=10,
        )
        send_btn  = gr.Button("Send ⚡", variant="primary", scale=1, min_width=80, elem_classes=["send-btn"])
        clear_btn = gr.Button("Clear",   scale=1, min_width=60, elem_classes=["clear-btn"])

    outputs = [msg_box, chatbot, chart_panel, suggestions_state, csv_btn] + sug_btns

    def _respond(msg, hist, sugg):
        """Same as respond() but also updates the CSV download button after a screen runs."""
        if not msg.strip():
            yield ("", hist, gr.update(), sugg, gr.update()) + tuple(gr.update() for _ in range(4))
            return

        clean = msg.lstrip("📊📋📈📰🔀🌍❓✅🔍 ").strip()
        loading_hist = hist + [
            {"role": "user",      "content": msg},
            {"role": "assistant", "content": LOADING_HTML},
        ]
        yield ("", loading_hist, gr.update(value=None, visible=False), sugg, gr.update()) + _make_btn_updates(sugg)

        text     = ""
        new_sugg = sugg
        csv_update = gr.update(visible=False)
        try:
            agent = get_agent("screener")
            text, _ = agent.process(clean)
            new_sugg = agent.generate_suggestions(text, clean)
            csv_path = agent.export_csv()
            if csv_path:
                csv_update = gr.update(value=csv_path, label="⬇ Export Results CSV", visible=True)
        except ValueError as e:
            text = (
                f"### ⚠️ API Key Required\n\n{e}\n\n"
                "Add to `.env`:\n"
                "```\nGROQ_API_KEY_1=your_key\nGEMINI_API_KEY=your_key\n```\n"
                "Then restart: `python app.py`"
            )
        except Exception as e:
            text = f"⚠️ Error: `{str(e)}`\n\nPlease try again."

        updated_hist = hist + [
            {"role": "user",      "content": msg},
            {"role": "assistant", "content": text},
        ]
        yield ("", updated_hist, gr.update(visible=False), new_sugg, csv_update) + _make_btn_updates(new_sugg)

    def _clear():
        result = clear_tab(tab_id)
        hist, chart, sugg = result[0], result[1], result[2]
        btns = result[3:]
        return [hist, chart, sugg, gr.update(value=None, visible=False)] + list(btns)

    msg_box.submit(_respond,  [msg_box, chatbot, suggestions_state], outputs)
    send_btn.click(_respond,  [msg_box, chatbot, suggestions_state], outputs)
    clear_btn.click(
        _clear, [],
        [chatbot, chart_panel, suggestions_state, csv_btn] + sug_btns,
    )

    for btn in sug_btns:
        btn.click(
            fn=lambda btn_val: (btn_val or "").strip(),
            inputs=[btn],
            outputs=[msg_box],
        )


# ── Watchlist helpers ─────────────────────────────────────────────────────────

def _fetch_watchlist_table() -> list[list]:
    """Return rows for the watchlist dataframe: Symbol, Name, Price, Change%, RSI, 52W High, 52W Low."""
    from utils.db import watchlist_all
    from tools.data.fetcher import get_info, get_ohlcv
    from tools.analysis.technical import compute_all, snapshot

    symbols = watchlist_all()
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


def _build_watchlist_tab():
    """Watchlist tab: add/remove symbols, live price table with RSI, refresh button."""
    from utils.db import watchlist_add, watchlist_remove, watchlist_all
    from utils.helpers import clean_ticker

    COLS = ["Symbol", "Name", "Price", "Change %", "RSI", "52W High", "52W Low"]

    gr.Markdown("## 📌 Watchlist\nTrack live prices and key indicators for your saved symbols.")

    # Price table
    wl_table = gr.Dataframe(
        value=_fetch_watchlist_table(),
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

    def _add(sym_raw):
        sym = clean_ticker(sym_raw.strip().upper())
        if not sym:
            return _fetch_watchlist_table(), "⚠️ Enter a valid NSE symbol."
        watchlist_add(sym)
        return _fetch_watchlist_table(), f"✅ **{sym}** added to watchlist."

    def _remove(sym_raw):
        sym = clean_ticker(sym_raw.strip().upper())
        if not sym:
            return _fetch_watchlist_table(), "⚠️ Enter a symbol to remove."
        watchlist_remove(sym)
        return _fetch_watchlist_table(), f"🗑️ **{sym}** removed from watchlist."

    def _refresh():
        rows = _fetch_watchlist_table()
        n = len(rows)
        return rows, f"🔄 Refreshed {n} symbol{'s' if n != 1 else ''}."

    add_btn.click(_add,     inputs=[add_box],  outputs=[wl_table, status])
    rem_btn.click(_remove,  inputs=[rem_box],  outputs=[wl_table, status])
    ref_btn.click(_refresh, inputs=[],         outputs=[wl_table, status])
    add_box.submit(_add,    inputs=[add_box],  outputs=[wl_table, status])


# ── Build full UI ─────────────────────────────────────────────────────────────

def build_ui() -> gr.Blocks:
    with gr.Blocks(title="ARIA — Professional Investment Research") as demo:

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

        # Tabs
        with gr.Tabs():
            with gr.Tab("📊 Equity Research"):
                _build_equity_tab()

            with gr.Tab("📋 Report Generator"):
                _build_report_tab()

            with gr.Tab("📈 Mutual Funds"):
                _build_tab("mf")

            with gr.Tab("🔍 Screener"):
                _build_screener_tab()

            with gr.Tab("📌 Watchlist"):
                _build_watchlist_tab()

        # Footer
        gr.HTML("""
        <div style='text-align:center;font-size:10px;color:#484f58;padding:6px 0 10px'>
          Data: NSE/BSE via yfinance · NSE bhavcopy · AMFI · Google News RSS — All free &nbsp;|&nbsp;
          ⚠️ Not SEBI-registered. For professional research purposes only.
        </div>
        """)

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
    )
