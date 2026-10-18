"""ui/portfolio.py — the Portfolio tab (UX-05): holdings at live prices, lots, CSV import."""

from __future__ import annotations

import gradio as gr

from services import portfolio as pf
from ui.auth import request_user
from ui.theme import page_header
from ui.watchlist import WATCHLIST_CONCURRENCY
from utils.escaping import html_text
from utils.numbers import format_inr

PORTFOLIO_LOT_COLS = ["Lot #", "Symbol", "Qty", "Avg price", "Buy date"]


def portfolio_lots_html(lots: list[dict]) -> str:
    """Plain, escaped HTML table of lots (gr.Dataframe paints one row — see
    watchlist_table_html)."""
    if not lots:
        return "<p class='wl-empty'>No holdings yet. Add one below or import your broker's CSV.</p>"
    classes = ("num", "sym", "num", "num", "")  # figures right-aligned, symbol in mono
    head = "".join(
        f"<th class='{k}'>{html_text(c)}</th>" for k, c in zip(classes, PORTFOLIO_LOT_COLS, strict=True)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f"<td class='{k}'>{html_text(cell)}</td>"
            for k, cell in zip(
                classes,
                (
                    lot["id"],
                    lot["symbol"],
                    f"{lot['qty'].normalize():f}",
                    format_inr(lot["avg_price"]),
                    lot["buy_date"] or "—",
                ),
                strict=True,
            )
        )
        + "</tr>"
        for lot in lots
    )
    return f"<div class='table-scroll'><table class='wl-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def portfolio_view(user_id: int | None):
    """(summary markdown, chart, lots html) for the Portfolio tab."""
    if not user_id:
        return "", None, portfolio_lots_html([])
    summary_md, chart, lots = pf.view(user_id)
    return summary_md, chart, portfolio_lots_html(lots)


def build_portfolio_tab(user_state: gr.State):
    """Portfolio tab: holdings at live prices (P&L, weights, allocation
    chart), add / edit / remove lots, and CSV import from a broker holdings
    export (Zerodha, Groww) or a plain sheet."""
    with gr.Row(elem_classes=["toolbar"]):
        gr.HTML(
            page_header(
                "Portfolio",
                "Your holdings at live prices. Quantities and prices are stored exactly; "
                "P&amp;L is before tax and charges.",
            )
        )
        ref_btn = gr.Button(
            "Refresh prices", size="sm", scale=0, min_width=0, elem_classes=["aria-ico", "ico-refresh"]
        )
    pf_refresh = gr.State(0)

    @gr.render(inputs=[user_state, pf_refresh])
    def _render(user_id, _refresh):
        summary_md, chart, lots_html = portfolio_view(user_id)
        gr.Markdown(summary_md, elem_id="pf-summary")
        if chart is not None:
            gr.Plot(chart, show_label=False, elem_id="pf-chart")
        with gr.Accordion("Lots", open=False):
            gr.HTML(lots_html, elem_id="pf-lots")

    status = gr.Markdown("", elem_id="pf-status", elem_classes=["status-line"])

    with gr.Column(elem_classes=["aria-panel"]):
        gr.HTML("<span class='eyebrow'>Add a holding</span>")
        with gr.Row(elem_classes=["toolbar"]):
            add_sym = gr.Textbox(label="Company or NSE symbol", max_lines=1, max_length=60, scale=3)
            add_qty = gr.Textbox(label="Quantity", max_lines=1, max_length=30, scale=1)
            add_price = gr.Textbox(label="Avg buy price (₹)", max_lines=1, max_length=30, scale=1)
            add_date = gr.Textbox(label="Buy date (optional)", placeholder="YYYY-MM-DD", max_lines=1, scale=1)
            add_btn = gr.Button(
                "Add holding", variant="primary", scale=1, min_width=80, elem_classes=["aria-ico", "ico-plus"]
            )

    with gr.Accordion("Edit or remove", open=False):
        with gr.Row():
            lot_id = gr.Number(label="Lot #", precision=0, scale=1)  # unknown lots are refused by the service
            ed_qty = gr.Textbox(label="New quantity", max_lines=1, max_length=30, scale=1)
            ed_price = gr.Textbox(label="New avg price (₹)", max_lines=1, max_length=30, scale=1)
            ed_date = gr.Textbox(label="Buy date", placeholder="YYYY-MM-DD", max_lines=1, scale=1)
        with gr.Row(elem_classes=["toolbar"]):
            upd_btn = gr.Button("Update lot", size="sm", elem_classes=["aria-ico", "ico-pen"])
            del_lot_btn = gr.Button(
                "Remove lot", size="sm", variant="stop", elem_classes=["aria-ico", "ico-trash"]
            )
            del_sym = gr.Textbox(label="", placeholder="Symbol", show_label=False, max_lines=1, scale=2)
            del_sym_btn = gr.Button(
                "Remove all lots of symbol", size="sm", variant="stop", elem_classes=["aria-ico", "ico-trash"]
            )

    with gr.Accordion("Import from your broker (CSV)", open=False):
        gr.Markdown(
            "Upload a holdings export: Zerodha Console (*Instrument, Qty., Avg. cost*), Groww "
            "(*Stock Name / ISIN, Quantity, Average buy price*) or any sheet with *symbol, quantity, "
            "avg price* (and optionally *buy date*) columns."
        )
        with gr.Row():
            csv_file = gr.File(label="Holdings CSV", file_types=[".csv"], scale=3)
            csv_replace = gr.Checkbox(label="Replace my current holdings", value=False, scale=1)
            csv_btn = gr.Button("Import", variant="primary", scale=1, elem_classes=["aria-ico", "ico-upload"])

    def _who(user_id, request):
        return request_user(request) or user_id

    def _add(user_id, sym, qty, price, when, refresh, request: gr.Request):
        user_id = _who(user_id, request)
        if not user_id:
            return refresh, "⚠️ Please sign in."
        try:
            symbol, quantity = pf.add_lot(user_id, sym, qty, price, when)
        except pf.UserInputError as e:
            return refresh, f"⚠️ {html_text(e.user_message)}"
        except pf.PortfolioFull:
            return refresh, f"⚠️ You can keep up to {pf.MAX_PORTFOLIO_LOTS} lots. Remove some to add more."
        return refresh + 1, f"✅ Added {quantity.normalize():f} **{symbol}**."

    def _update(user_id, lid, qty, price, when, refresh, request: gr.Request):
        user_id = _who(user_id, request)
        try:
            ok = pf.update_lot(user_id, lid, qty, price, when)
        except pf.UserInputError as e:
            return refresh, f"⚠️ {html_text(e.user_message)}"
        return (refresh + 1, f"✎ Lot #{int(lid)} updated.") if ok else (refresh, "⚠️ No such lot.")

    def _remove_lot(user_id, lid, refresh, request: gr.Request):
        user_id = _who(user_id, request)
        ok = pf.remove_lot(user_id, lid)
        return (refresh + 1, f"🗑 Lot #{int(lid)} removed.") if ok else (refresh, "⚠️ No such lot.")

    def _remove_symbol(user_id, sym, refresh, request: gr.Request):
        user_id = _who(user_id, request)
        sym, n = pf.remove_symbol(user_id, sym)
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
            n, problems = pf.import_csv(user_id, path, bool(replace))
        except pf.UserInputError as e:
            return refresh, f"⚠️ {html_text(e.user_message)}"
        except pf.PortfolioFull:
            return refresh, f"⚠️ That would take you past {pf.MAX_PORTFOLIO_LOTS} lots."
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
