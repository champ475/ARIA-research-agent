"""ui/admin.py — the Admin tab (OBS-05): user lookup, recent turns, a user's alerts,
feature flags and disabling an account. Visible only to admins, and every handler
checks again on the server (services/admin.py::require_admin) — hiding the tab is
courtesy, not the control."""

from __future__ import annotations

import gradio as gr

from services import admin as svc
from ui.auth import request_user
from utils.escaping import html_text, md_cell
from utils.flags import FLAGS


def _table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return "*Nothing to show.*"
    head = "| " + " | ".join(label for _, label in columns) + " |\n|" + "---|" * len(columns)
    body = "\n".join("| " + " | ".join(md_cell(str(r.get(k, "—"))) for k, _ in columns) + " |" for r in rows)
    return f"{head}\n{body}"


TURN_COLS = [
    ("when", "When"),
    ("user_id", "User"),
    ("intent", "Intent"),
    ("status", "Status"),
    ("latency_s", "Seconds"),
    ("llm_calls", "LLM calls"),
    ("tokens", "Tokens"),
    ("cost_inr", "₹"),
    ("gaps", "Gaps"),
    ("error_id", "Error ID"),
    ("trace", "Trace"),
]
ALERT_COLS = [
    ("id", "#"),
    ("condition", "Condition"),
    ("status", "Status"),
    ("last_checked", "Last checked"),
    ("last_fired", "Last fired"),
    ("delivery", "Delivery"),
    ("attempts", "Attempts"),
    ("last_error", "Last error"),
]


def user_card(u: dict | None) -> str:
    if u is None:
        return "No account with that email."
    flags = [
        "verified" if u["verified"] else "**not verified**",
        "**DISABLED**" if u["disabled"] else "active",
        "admin" if u["is_admin"] else "",
    ]
    return (
        f"**{html_text(u['email'])}** · id **{u['id']}** · signed up {u['signed_up']}  \n"
        f"{', '.join(f for f in flags if f)}  \n"
        f"Quota: {u['quota']} · Telegram: {'linked' if u['telegram_linked'] else 'not linked'} · "
        f"Active alerts: {u['active_alerts']} · Live sessions: {u['live_sessions']} · Last turn: {u['last_turn']}"
    )


def build_admin_tab(user_state: gr.State, tab: gr.Tab):
    gr.Markdown(
        "## 🛠 Admin\nFor operators. Everything you look up or change here is written to the audit log."
    )
    note = gr.Markdown("", elem_id="admin-note")

    def _who(user_id, request):
        return request_user(request) or user_id

    # tab visibility: only admins see it (the server-side check is in services/admin.py)
    def _show(user_id, request: gr.Request):
        return gr.update(visible=svc.is_admin(_who(user_id, request)))

    user_state.change(_show, [user_state], [tab], show_progress="hidden")

    # ── Look up a user ────────────────────────────────────────────────────────
    with gr.Row():
        email_box = gr.Textbox(
            label="User email", max_lines=1, max_length=320, scale=3, elem_id="admin-email"
        )
        find_btn = gr.Button("Find", variant="primary", scale=1, elem_id="admin-find")
    card = gr.Markdown("", elem_id="admin-card")
    found_id = gr.State(None)
    with gr.Row():
        turns_btn = gr.Button("Recent turns", size="sm", elem_id="admin-turns")
        alerts_btn = gr.Button("Their alerts", size="sm", elem_id="admin-alerts")
        disable_btn = gr.Button("Disable account", size="sm", variant="stop", elem_id="admin-disable")
        enable_btn = gr.Button("Enable account", size="sm", elem_id="admin-enable")
    detail = gr.Markdown("", elem_id="admin-detail")

    def _find(user_id, email, request: gr.Request):
        try:
            u = svc.find_user(_who(user_id, request), email)
        except svc.Forbidden as e:
            return f"⛔ {html_text(e.user_message)}", None
        return user_card(u), (u["id"] if u else None)

    def _turns(user_id, uid, request: gr.Request):
        try:
            return _table(svc.recent_turns(_who(user_id, request), uid), TURN_COLS)
        except svc.Forbidden as e:
            return f"⛔ {html_text(e.user_message)}"

    def _alerts(user_id, uid, request: gr.Request):
        if not uid:
            return "Find a user first."
        try:
            return _table(svc.alerts_of(_who(user_id, request), int(uid)), ALERT_COLS)
        except svc.Forbidden as e:
            return f"⛔ {html_text(e.user_message)}"

    def _set_disabled(disabled: bool):
        def run(user_id, uid, request: gr.Request):
            if not uid:
                return "Find a user first."
            try:
                ok = svc.set_disabled(_who(user_id, request), int(uid), disabled)
            except svc.Forbidden as e:
                return f"⛔ {html_text(e.user_message)}"
            if not ok:
                return "No such user."
            return f"User {int(uid)} {'disabled; their sessions were ended' if disabled else 'enabled'}."

        return run

    find_btn.click(_find, [user_state, email_box], [card, found_id])
    email_box.submit(_find, [user_state, email_box], [card, found_id])
    turns_btn.click(_turns, [user_state, found_id], [detail])
    alerts_btn.click(_alerts, [user_state, found_id], [detail])
    disable_btn.click(_set_disabled(True), [user_state, found_id], [detail])
    enable_btn.click(_set_disabled(False), [user_state, found_id], [detail])

    # ── Feature flags ─────────────────────────────────────────────────────────
    gr.Markdown("### Feature flags")
    flags_md = gr.Markdown("", elem_id="admin-flags")
    with gr.Row():
        flag_key = gr.Dropdown(choices=sorted(FLAGS), label="Flag", scale=2, elem_id="admin-flag-key")
        flag_on = gr.Button("Turn on", size="sm", elem_id="admin-flag-on")
        flag_off = gr.Button("Turn off", size="sm", elem_id="admin-flag-off")
        flag_refresh = gr.Button("Refresh", size="sm")

    def _flags_view(user_id, request: gr.Request):
        try:
            rows = svc.flag_list(_who(user_id, request))
        except svc.Forbidden as e:
            return f"⛔ {html_text(e.user_message)}"
        return _table(
            [{**r, "enabled": "on" if r["enabled"] else "OFF"} for r in rows],
            [("key", "Flag"), ("enabled", "State"), ("description", "What it does")],
        )

    def _flag_set(value: bool):
        def run(user_id, key, request: gr.Request):
            if not key:
                return "Pick a flag.", gr.update()
            try:
                svc.set_flag(_who(user_id, request), key, value)
            except svc.Forbidden as e:
                return f"⛔ {html_text(e.user_message)}", gr.update()
            return f"{key} turned {'on' if value else 'off'}.", _flags_view(user_id, request)

        return run

    flag_refresh.click(_flags_view, [user_state], [flags_md])
    flag_on.click(_flag_set(True), [user_state, flag_key], [note, flags_md])
    flag_off.click(_flag_set(False), [user_state, flag_key], [note, flags_md])
    tab.select(_flags_view, [user_state], [flags_md], show_progress="hidden")
    return note
