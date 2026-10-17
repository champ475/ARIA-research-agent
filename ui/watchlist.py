"""ui/watchlist.py — the Watchlist tab: add/remove symbols and a live price table."""

from __future__ import annotations

import gradio as gr

from services import watchlist as wl
from utils.errors import UserInputError
from utils.escaping import html_text

WATCHLIST_COLS = ["Symbol", "Name", "Price", "Change %", "RSI", "52W High", "52W Low"]
WATCHLIST_CONCURRENCY = 4  # refresh clicks at once; one yfinance call per symbol, keep it bounded


def watchlist_table_html(rows: list[list]) -> str:
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


def build_watchlist_tab(user_state: gr.State):
    """
    Watchlist tab: add/remove symbols, live price table with RSI, refresh
    button. The table lives inside @gr.render() keyed to a refresh counter
    (bumped on every add/remove/refresh) so it re-renders on every change —
    see watchlist_table_html() for why it's plain HTML, not gr.Dataframe.
    """
    gr.Markdown("## 📌 Watchlist\nTrack live prices and key indicators for your saved symbols.")

    wl_refresh = gr.State(0)

    @gr.render(inputs=[user_state, wl_refresh])
    def _render_table(user_id, _refresh):
        rows = wl.table_rows(user_id) if user_id else []
        gr.HTML(watchlist_table_html(rows), elem_id="wl-table")

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
        try:
            added, sym = wl.add_symbol(user_id, sym_raw)
        except UserInputError as e:
            return refresh, f"⚠️ {html_text(e.user_message)}"
        except wl.WatchlistFull:
            return (
                refresh,
                f"⚠️ Your watchlist is full ({wl.WATCHLIST_MAX} symbols). Remove one to add another.",
            )
        if not added:
            return refresh, f"ℹ️ **{sym}** is already on your watchlist."
        return refresh + 1, f"✅ **{sym}** added to watchlist."

    def _remove(user_id, sym_raw, refresh):
        sym = wl.remove_symbol(user_id, sym_raw)
        if not sym:
            return refresh, "⚠️ Enter a symbol to remove."
        return refresh + 1, f"🗑️ **{sym}** removed from watchlist."

    def _refresh_click(user_id, refresh):
        n = len(wl.table_rows(user_id))
        return refresh + 1, f"🔄 Refreshed {n} symbol{'s' if n != 1 else ''}."

    add_btn.click(_add, inputs=[user_state, add_box, wl_refresh], outputs=[wl_refresh, status])
    rem_btn.click(_remove, inputs=[user_state, rem_box, wl_refresh], outputs=[wl_refresh, status])
    ref_btn.click(
        _refresh_click,
        inputs=[user_state, wl_refresh],
        outputs=[wl_refresh, status],
        concurrency_limit=WATCHLIST_CONCURRENCY,
    )
    add_box.submit(_add, inputs=[user_state, add_box, wl_refresh], outputs=[wl_refresh, status])

    return status
