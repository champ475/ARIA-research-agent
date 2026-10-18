"""ui/alerts.py — the Alerts tab (UX-06): every alert's condition, status, last check
and delivery, with pause, resume, delete and edit. Alerts are *created* in chat."""

from __future__ import annotations

import gradio as gr

from services import alerts as svc
from ui.auth import request_user
from ui.theme import page_header
from utils.escaping import html_text

ALERT_COLS = ["#", "Symbol", "Condition", "Status", "Last fired", "Last checked", "Delivery"]


def alerts_table_html(rows: list[dict]) -> str:
    """Plain, escaped HTML table (gr.Dataframe paints one row — see watchlist_table_html)."""
    if not rows:
        return (
            "<p class='wl-empty'>No alerts yet. Ask in the chat, for example "
            "<em>“Alert me if INFY falls 5%”</em> or <em>“Alert me if TCS goes above 4000”</em>.</p>"
        )
    head = "".join(f"<th>{html_text(c)}</th>" for c in ALERT_COLS)
    body = ""
    for r in rows:
        cells = (
            r["id"],
            r["symbol"],
            r["condition"] + (" · recurring" if r["recurring"] else ""),
            r["status"],
            r["last_fired"],
            r["last_checked"],
            r["delivery"],
        )
        tds = [f"<td>{html_text(c)}</td>" for c in cells]
        tds[0] = f"<td class='num'>{html_text(cells[0])}</td>"
        tds[1] = f"<td class='sym'>{html_text(cells[1])}</td>"
        # Status reads at a glance: active alerts in the accent, paused ones muted.
        state = "on" if str(r["status"]).lower().startswith("active") else "off"
        tds[3] = f"<td><span class='state-pill {state}'>{html_text(cells[3])}</span></td>"
        body += "<tr>" + "".join(tds) + "</tr>"
    return (
        "<div class='table-scroll'><table class='wl-table' id='alerts-table'>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def build_alerts_tab(user_state: gr.State, tab: gr.Tab):
    gr.HTML(
        page_header(
            "Alerts",
            "Everything you've asked ARIA to watch. Create alerts in the <strong>Chat</strong> tab and "
            "manage them here. Checks run every 5 minutes while the market is open.",
        )
    )
    refresh = gr.State(0)

    @gr.render(inputs=[user_state, refresh])
    def _render(user_id, _refresh):
        rows = svc.rows(user_id) if user_id else []
        gr.HTML(alerts_table_html(rows), elem_id="alerts-table-wrap")

    status = gr.Markdown("", elem_id="alerts-status", elem_classes=["status-line"])

    with gr.Row(elem_classes=["toolbar"]):
        alert_id = gr.Number(
            label="Alert #", precision=0, scale=1, elem_id="alert-id"
        )  # 0 / unknown ids are refused by _act
        pause_btn = gr.Button(
            "Pause", size="sm", elem_id="alert-pause", elem_classes=["aria-ico", "ico-pause"]
        )
        resume_btn = gr.Button(
            "Resume", size="sm", elem_id="alert-resume", elem_classes=["aria-ico", "ico-play"]
        )
        delete_btn = gr.Button(
            "Delete",
            size="sm",
            variant="stop",
            elem_id="alert-delete",
            elem_classes=["aria-ico", "ico-trash"],
        )
        refresh_btn = gr.Button("Refresh", size="sm", elem_classes=["aria-ico", "ico-refresh"])
    with gr.Accordion("Change the number", open=False):
        gr.Markdown("A price in ₹, a percent, an RSI level or a volume multiple, whatever the alert watches.")
        with gr.Row():
            new_value = gr.Textbox(
                label="New value", max_lines=1, max_length=30, scale=2, elem_id="alert-value"
            )
            recurring = gr.Radio(
                ["Keep as is", "Fire every time", "Fire once"],
                value="Keep as is",
                label="Repeat",
                scale=2,
            )
            save_btn = gr.Button(
                "Save",
                variant="primary",
                scale=1,
                elem_id="alert-save",
                elem_classes=["aria-ico", "ico-save"],
            )

    def _who(user_id, request):
        return request_user(request) or user_id

    def _act(fn):
        def run(user_id, aid, refresh_n, request: gr.Request):
            user_id = _who(user_id, request)
            if not user_id:
                return refresh_n, "⚠️ Please sign in."
            if not aid:
                return refresh_n, "⚠️ Enter the alert number from the table."
            return fn(user_id, int(aid), refresh_n)

        return run

    def _pause(user_id, aid, n):
        ok = svc.set_active(user_id, aid, False)
        return (n + 1, f"⏸ Alert #{aid} paused.") if ok else (n, "⚠️ No such alert.")

    def _resume(user_id, aid, n):
        try:
            ok = svc.set_active(user_id, aid, True)
        except svc.DuplicateAlert:
            return n, "⚠️ An identical alert is already active."
        return (n + 1, f"▶ Alert #{aid} is active again.") if ok else (n, "⚠️ No such alert.")

    def _delete(user_id, aid, n):
        ok = svc.delete(user_id, aid)
        return (n + 1, f"🗑 Alert #{aid} deleted.") if ok else (n, "⚠️ No such alert.")

    def _save(user_id, aid, value, repeat, n, request: gr.Request):
        user_id = _who(user_id, request)
        if not user_id:
            return n, "⚠️ Please sign in."
        if not aid:
            return n, "⚠️ Enter the alert number from the table."
        flag = {"Fire every time": True, "Fire once": False}.get(repeat)
        try:
            text = svc.edit(user_id, int(aid), value, recurring=flag)
        except svc.UserInputError as e:
            return n, f"⚠️ {html_text(e.user_message)}"
        except svc.DuplicateAlert:
            return n, "⚠️ An identical alert is already active."
        except LookupError:
            return n, "⚠️ No such alert."
        return n + 1, f"💾 Saved: {html_text(text)}."

    pause_btn.click(_act(_pause), [user_state, alert_id, refresh], [refresh, status])
    resume_btn.click(_act(_resume), [user_state, alert_id, refresh], [refresh, status])
    delete_btn.click(_act(_delete), [user_state, alert_id, refresh], [refresh, status])
    save_btn.click(_save, [user_state, alert_id, new_value, recurring, refresh], [refresh, status])
    refresh_btn.click(lambda n: n + 1, [refresh], [refresh])
    # Alerts are created in the chat, so the table is read afresh whenever this tab is opened.
    tab.select(lambda n: n + 1, [refresh], [refresh], show_progress="hidden")
    return status
