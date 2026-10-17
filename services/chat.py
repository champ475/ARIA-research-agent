"""
services/chat.py — what the chat tab needs from the rest of ARIA (ARCH-02).

No Gradio in here. The UI (ui/chat.py) calls these and renders the results:

- the per-conversation orchestrator cache and the one-turn-at-a-time lock;
- the per-user turn rate limit (SEC-12);
- saving an exchange, session titles, and the chat sessions list;
- answer feedback (LLM-12) and the first-run card (UX-01);
- the small text helpers for routing labels and download links.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cachetools import TTLCache

from config import TURNS_PER_MINUTE

if TYPE_CHECKING:
    from agents.graph_orchestrator import GraphOrchestrator

log = logging.getLogger(__name__)

# ── Lazy orchestrator loader ──────────────────────────────────────────────────
# One GraphOrchestrator per conversation (agents/graph_orchestrator.py), the
# LangGraph research engine; see RFC_AGENTIC_RESEARCH_GRAPH.md.
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


def get_orchestrator(user_id: int, session_id: int | None = None) -> GraphOrchestrator:
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


def confirmation_pending(user_id: int | None, session_id: int | None) -> bool:
    """True while this chat's graph is paused waiting for a yes/no (VAL-08)."""
    orch = _orchestrators.get((user_id, session_id)) if user_id else None
    return bool(orch and getattr(orch, "_awaiting_confirmation", False))


# ── Concurrency (REL-01) ──────────────────────────────────────────────────────
# Gradio defaults every event to concurrency_limit=1, so one user's 30-second
# report used to queue every other user's message behind it. Chat turns now
# share a pool of CHAT_CONCURRENCY workers; each conversation still gets at
# most ONE turn in flight, because its GraphOrchestrator (and conversation
# memory) is per-conversation state that isn't safe to drive from two threads
# at once — a second tab's message gets a "still working" reply.
_user_turn_locks: TTLCache = TTLCache(maxsize=ORCHESTRATOR_CACHE_SIZE * 2, ttl=ORCHESTRATOR_IDLE_SECONDS)
_user_turn_locks_guard = threading.Lock()


def user_turn_lock(user_id: int | None, session_id: int | None = None) -> threading.Lock:
    with _user_turn_locks_guard:
        key = (user_id, session_id)
        lock = _user_turn_locks.get(key)
        if lock is None:
            lock = threading.Lock()
        _user_turn_locks[key] = lock
        return lock


def turn_allowed(user_id: int | None) -> bool:
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


# ── Timeframe keyword (appended to the message when non-default, equity only) ─
TF_KEYWORDS = {
    "1D": "",
    "1W": "weekly",
    "1M": "monthly",
    "3M": "3mo",
    "6M": "6mo",
    "2Y": "2y",
}


def with_timeframe(user_id: int, session_id: int | None, clean: str, tf: str) -> str:
    """Append the chart-timeframe keyword when equity handles the message, so it
    isn't injected into an unrelated domain. Asked of the SAME orchestrator
    that will process the message, so this decision and the real routing can
    never disagree. Any failure here (e.g. a missing API key) is swallowed: the
    real process_stream() call re-attempts construction and surfaces the same
    failure as a proper "API Key Required" message."""
    try:
        if get_orchestrator(user_id, session_id).classify(clean) == "equity":
            kw = TF_KEYWORDS.get(tf, "")
            if kw and kw not in clean.lower():
                return clean + " " + kw
    except Exception:
        pass
    return clean


# ── Labels and links ──────────────────────────────────────────────────────────
TAB_LABELS = {
    "equity": "📊 Equity Research",
    "report": "📋 Report Generator",
    "mf": "📈 Mutual Funds",
    "screener": "🔍 Screener",
    "alert": "🔔 Alerts",
    "planning": "🧭 Financial Planner",
}


def tab_label(tab_used: str) -> str:
    """Renders a routing badge label for `tab_used`, including a joined
    multi-domain label (e.g. "report+alert" -> "📋 Report Generator + 🔔
    Alerts") when the graph orchestrator names more than one domain for this
    turn (Phase 7.4 multi-intent support) — falls back to the raw segment
    for any domain name not in TAB_LABELS."""
    parts = tab_used.split("+")
    return " + ".join(TAB_LABELS.get(p, p) for p in parts)


def routing_badge(tab_used: str | None) -> str:
    """UX-02: the "Routed to" line is a debugging aid, off unless SHOW_ROUTING_BADGE."""
    from config import SHOW_ROUTING_BADGE

    return f"*🔀 Routed to: {tab_label(tab_used or 'equity')}*\n\n" if SHOW_ROUTING_BADGE else ""


_DOWNLOAD_URL_RE = re.compile(r"^/download/(?:report/\d+\.(?:pdf|md)|file/[A-Za-z0-9_-]{16,64})$")


def download_link(url: str, label: str) -> str:
    """A Markdown link to one of our own download routes (anything else renders nothing)."""
    if not _DOWNLOAD_URL_RE.match(url or ""):
        return ""
    return f"[{label}]({url})"


def download_links(pdf_url: str | None, md_url: str | None, csv_url: str | None) -> str:
    """OPS-02: the answer's download links, as a line under it ("" if none)."""
    links = [
        download_link(pdf_url or "", "⬇ Download PDF"),
        download_link(md_url or "", "⬇ Download Markdown"),
        download_link(csv_url or "", "⬇ Export results (CSV)"),
    ]
    links = [x for x in links if x]
    return "\n\n" + " · ".join(links) if links else ""


# ── Sessions and saved exchanges ──────────────────────────────────────────────


def session_title(msg: str) -> str:
    """First-message-derived title for a new chat session, ChatGPT-style."""
    t = " ".join(msg.strip().split())
    if not t:
        return "New chat"
    return (t[:42] + "…") if len(t) > 42 else t


def persist_turn(
    user_id: int, session_id: int, user_msg: str, assistant_msg: str, is_first: bool, route: str | None = None
) -> None:
    """Saves the exchange. The assistant row carries its turn id (feedback and
    traces link to it, LLM-12) and the route in `meta`; the stored text has no
    routing badge, so it never reaches the model as history."""
    from agents.conversation_memory import strip_badge
    from utils.db import chat_save, session_touch
    from utils.logging_setup import turn_id_var

    meta = {"turn_id": turn_id_var.get() or None, "route": route}
    chat_save(user_id, session_id, "chat", "user", user_msg)
    chat_save(user_id, session_id, "chat", "assistant", strip_badge(assistant_msg), meta=meta)
    session_touch(user_id, session_id, title=session_title(user_msg) if is_first else None)


def create_session(user_id: int, tab: str, first_message: str) -> int:
    from utils.db import session_create

    return session_create(user_id, tab, title=session_title(first_message))


def list_sessions(user_id: int, tab: str) -> list[dict]:
    from utils.db import session_list

    return session_list(user_id, tab)


def load_session(user_id: int, session_id: int) -> list[dict]:
    """The saved messages of a chat, as chatbot history. Also wakes the chat's
    orchestrator, which restores its own memory (and any pending confirmation)
    from the database (REL-03/VAL-10)."""
    from utils.db import chat_load

    msgs = chat_load(user_id, session_id)
    get_orchestrator(user_id, session_id)
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


def delete_session(user_id: int, session_id: int) -> None:
    from utils.db import session_delete

    session_delete(user_id, session_id)


# ── Answer feedback (LLM-12) ──────────────────────────────────────────────────


def record_like(user_id: int, session_id: int, index: Any, liked: bool | None) -> tuple[str, str | None]:
    """A thumbs up/down on the answer at `index` of a chat. Returns
    ("up" | "down" | "ignored", turn_id)."""
    from utils.db import chat_load, feedback_record

    rows = chat_load(user_id, session_id)
    if not isinstance(index, int) or index >= len(rows) or rows[index]["role"] != "assistant":
        return "ignored", None
    turn_id = (rows[index].get("meta") or {}).get("turn_id")
    if not turn_id or liked is None:
        return "ignored", None
    rating = 1 if liked else -1
    feedback_record(user_id, turn_id, rating, session_id=session_id)
    from utils.metrics import observe_feedback

    observe_feedback(rating)
    return ("up" if rating == 1 else "down"), turn_id


def send_feedback(user_id: int, session_id: int | None, turn_id: str, reason: Any, details: Any) -> None:
    from utils.db import feedback_record

    feedback_record(user_id, turn_id, -1, session_id=session_id, reason=reason, free_text=details)


# ── First-run card (UX-01) ────────────────────────────────────────────────────


def onboarding_info(user_id: int) -> dict | None:
    """What the first-run card needs, or None if the user already dismissed it
    or the status could not be read."""
    from utils.db import user_onboarding
    from utils.helpers import IST

    try:
        info = user_onboarding(user_id, datetime.now(IST).date())
    except Exception:
        log.warning("[onboarding] status unavailable", exc_info=True)
        return None
    return None if info["onboarded"] else info


def dismiss_onboarding(user_id: int) -> None:
    from utils.db import user_mark_onboarded

    user_mark_onboarded(user_id)
