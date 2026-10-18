"""ui/watchlist.py — the Watchlist tab: add/remove symbols and a live price table."""

from __future__ import annotations

import gradio as gr

from services import watchlist as wl
from ui.theme import page_header
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
        return "<p class='wl-empty'>No symbols yet. Add one above, for example <em>TCS</em> or <em>RELIANCE</em>.</p>"
    # SEC-10: every cell is escaped — symbols come from users, names from yfinance.
    head = "".join(f"<th class='{_COL_CLASS[i]}'>{html_text(c)}</th>" for i, c in enumerate(WATCHLIST_COLS))
    body = "".join("<tr>" + "".join(_cell(i, cell) for i, cell in enumerate(row)) + "</tr>" for row in rows)
    return f"<div class='table-scroll'><table class='wl-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


# Column roles: the symbol is set in mono, the name wraps, every figure is right-aligned.
_COL_CLASS = ["sym", "name", "num", "num", "num", "num", "num"]


def _cell(i: int, value) -> str:
    """One escaped cell; the change column ("▲ +1.20%" / "▼ -0.80%") is tinted gain or loss."""
    text = html_text(value)
    if i == 3 and isinstance(value, str) and value[:1] in ("▲", "▼"):
        # an unchanged price ("▲ +0.00%") is neither a gain nor a loss
        tone = "flat" if value.endswith("0.00%") else ("up" if value[0] == "▲" else "dn")
        text = f"<span class='chg {tone}'>{text}</span>"
    return f"<td class='{_COL_CLASS[i] if i < len(_COL_CLASS) else ''}'>{text}</td>"


def build_watchlist_tab(user_state: gr.State):
    """
    Watchlist tab: add/remove symbols, live price table with RSI, refresh
    button. The table lives inside @gr.render() keyed to a refresh counter
    (bumped on every add/remove/refresh) so it re-renders on every change —
    see watchlist_table_html() for why it's plain HTML, not gr.Dataframe.
    """
    gr.HTML(page_header("Watchlist", "Live prices and key indicators for the symbols you follow."))

    wl_refresh = gr.State(0)

    # Add / Remove / Refresh, above the table they change
    with gr.Row(elem_classes=["toolbar"]):
        add_box = gr.Textbox(
            placeholder="Add symbol (e.g. TCS, RELIANCE)",
            label="",
            show_label=False,
            scale=4,
            lines=1,
            max_lines=1,
        )
        add_btn = gr.Button(
            "Add", variant="primary", scale=0, min_width=80, elem_classes=["aria-ico", "ico-plus"]
        )
        rem_box = gr.Textbox(
            placeholder="Remove symbol", label="", show_label=False, scale=3, lines=1, max_lines=1
        )
        rem_btn = gr.Button(
            "Remove", variant="secondary", scale=0, min_width=90, elem_classes=["aria-ico", "ico-minus"]
        )
        ref_btn = gr.Button(
            "Refresh", variant="secondary", scale=0, min_width=90, elem_classes=["aria-ico", "ico-refresh"]
        )

    # Status message
    status = gr.Markdown("", elem_classes=["status-line"])

    @gr.render(inputs=[user_state, wl_refresh])
    def _render_table(user_id, _refresh):
        rows = wl.table_rows(user_id) if user_id else []
        gr.HTML(watchlist_table_html(rows), elem_id="wl-table")

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
