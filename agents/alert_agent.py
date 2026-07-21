"""
agents/alert_agent.py
Chat-driven alert creation/management. Actual condition EVALUATION happens
in scripts/check_alerts.py (run on a schedule by GitHub Actions) — this
agent only does CRUD against the `alerts` table via natural language.
"""

import json
import re

from agents.llm_client import LLMClient
from config import TOKENS_AGENT, TOKENS_FAST
from utils.db import alert_add, alert_list, alert_remove, alert_pause, alert_resume
from utils.helpers import extract_entity, clean_ticker, validate_ticker

SYSTEM = """
You are ARIA Alerts — a focused assistant that helps users set up price and
technical-indicator alerts on Indian (NSE/BSE) stocks. You don't give
investment advice here; you just confirm what alert was created, list
existing alerts, or confirm cancellations, in clean Markdown.
"""

_ALERT_TYPES = [
    "price_above", "price_below", "pct_change",
    "rsi_cross_above", "rsi_cross_below",
    "macd_bullish_cross", "macd_bearish_cross",
    "ma_cross", "bb_breakout", "volume_spike",
    "week52_high", "week52_low", "bulk_deal", "portfolio_pnl",
]

_LIST_KW   = ["list my alert", "show my alert", "my alerts", "active alerts", "show alerts"]
_CANCEL_KW = ["cancel", "delete", "remove", "stop alert"]
_PAUSE_KW  = ["pause"]
_RESUME_KW = ["resume", "reactivate", "unpause"]

_PRICE_ABOVE_RE = re.compile(r"\b(above|over|exceeds?|crosses?\s+above|goes?\s+above)\b", re.I)
_PRICE_BELOW_RE = re.compile(r"\b(below|under|drops?\s+below|falls?\s+below)\b", re.I)
_NUM_RE = re.compile(r"[\d,]+\.?\d*")
_INDICATOR_HINT_KW = [
    "rsi", "macd", "bollinger", "bb ", "moving average", "sma", "ema",
    "volume", "52 week", "52-week", "bulk deal", "block deal",
    "portfolio", "p&l", "pnl", "% change", "percent change", "golden cross",
    "death cross",
]

_EXTRACT_PROMPT = """Extract the alert condition from this user message as JSON only, no prose, no markdown fences.
Message: "{message}"
Symbol already identified: {symbol}

Return exactly this JSON shape:
{{"alert_type": one of {types}, "threshold": number or null, "condition": "short human-readable summary"}}

Rules:
- price_above/price_below/pct_change/rsi_cross_above/rsi_cross_below/volume_spike need a numeric threshold.
- macd_bullish_cross/macd_bearish_cross/ma_cross/bb_breakout/week52_high/week52_low/bulk_deal/portfolio_pnl usually don't need one — use null.
- If you truly cannot determine a valid alert_type from the list, use null for alert_type.
"""


class AlertAgent:

    def __init__(self, user_id: int):
        self._client = LLMClient(system_prompt=SYSTEM)
        self._user_id = user_id

    @property
    def provider(self):
        return self._client.provider

    def process(self, message: str) -> tuple[str, None]:
        msg = message.strip()
        lower = msg.lower()

        if any(k in lower for k in _LIST_KW):
            return self._list_alerts(), None
        if any(k in lower for k in _CANCEL_KW):
            return self._cancel_alert(msg), None
        if any(k in lower for k in _PAUSE_KW):
            return self._pause_alert(msg), None
        if any(k in lower for k in _RESUME_KW):
            return self._resume_alert(msg), None
        return self._create_alert(msg), None

    # ── Create ───────────────────────────────────────────────────────────────

    def _create_alert(self, msg: str) -> str:
        symbol = extract_entity(msg)
        symbol = clean_ticker(symbol) if symbol else None
        if not symbol or not validate_ticker(symbol):
            return (
                "I couldn't tell which stock you mean. Try something like "
                "**\"alert me if TCS drops below 3500\"** or "
                "**\"notify me when RELIANCE RSI crosses 30\"**."
            )

        alert_type, threshold, condition = self._regex_extract(msg, symbol)
        if alert_type is None:
            alert_type, threshold, condition = self._llm_extract(msg, symbol)

        if alert_type is None or alert_type not in _ALERT_TYPES:
            return (
                f"I identified **{symbol}** but couldn't work out the exact condition. "
                "Could you rephrase — e.g. \"alert me if it drops below 3500\" or "
                "\"notify me on a MACD bullish crossover\"?"
            )

        alert_id = alert_add(self._user_id, symbol, alert_type, condition, threshold)
        return (
            f"✅ Alert created (#{alert_id})\n\n"
            f"**{symbol}** — {condition}\n\n"
            "You'll get a Telegram notification when this triggers. It fires once, then deactivates."
        )

    def _regex_extract(self, msg: str, symbol: str):
        """Cheap deterministic pre-filter for the two unambiguous plain-price
        cases — avoids an LLM call for the most common alert type. Bails out
        (forces the LLM path) if the message mentions any indicator/pattern
        vocabulary, since "RSI crosses below 30" contains "below" but is NOT
        a plain price_below alert."""
        lower = msg.lower()
        if any(kw in lower for kw in _INDICATOR_HINT_KW):
            return None, None, None
        nums = _NUM_RE.findall(msg.replace(",", ""))
        if not nums:
            return None, None, None
        threshold = float(nums[-1])
        if _PRICE_ABOVE_RE.search(msg):
            return "price_above", threshold, f"Price crosses above ₹{threshold:,.2f}"
        if _PRICE_BELOW_RE.search(msg):
            return "price_below", threshold, f"Price drops below ₹{threshold:,.2f}"
        return None, None, None

    def _llm_extract(self, msg: str, symbol: str):
        prompt = _EXTRACT_PROMPT.format(message=msg, symbol=symbol, types=_ALERT_TYPES)
        try:
            raw = self._client.ask(prompt, temperature=0.0, max_tokens=TOKENS_FAST)
            raw = raw.strip().strip("```json").strip("```").strip()
            data = json.loads(raw)
            alert_type = data.get("alert_type")
            threshold = data.get("threshold")
            condition = data.get("condition") or (alert_type or "condition met")
            if alert_type not in _ALERT_TYPES:
                return None, None, None
            return alert_type, (float(threshold) if threshold is not None else None), condition
        except Exception:
            return None, None, None

    # ── List / cancel / pause / resume ──────────────────────────────────────

    def _list_alerts(self) -> str:
        alerts = alert_list(self._user_id)
        if not alerts:
            return "You have no alerts yet. Try \"alert me if TCS drops below 3500\"."
        lines = ["## Your Alerts\n", "| # | Symbol | Condition | Status | Created |", "|---|--------|-----------|--------|---------|"]
        for a in alerts:
            status = "🟢 Active" if a["active"] else ("🔔 Triggered" if a["triggered_at"] else "⏸️ Paused")
            lines.append(f"| {a['id']} | {a['symbol']} | {a['condition']} | {status} | {a['created_at'].strftime('%Y-%m-%d')} |")
        return "\n".join(lines)

    def _match_alert(self, msg: str):
        symbol = extract_entity(msg)
        id_match = re.search(r"#?(\d+)", msg)
        alerts = alert_list(self._user_id, active_only=True)
        if id_match:
            aid = int(id_match.group(1))
            return next((a for a in alerts if a["id"] == aid), None), alerts
        if symbol:
            symbol = clean_ticker(symbol)
            matches = [a for a in alerts if a["symbol"] == symbol]
            if len(matches) == 1:
                return matches[0], alerts
            return None, matches
        return None, alerts

    def _cancel_alert(self, msg: str) -> str:
        target, candidates = self._match_alert(msg)
        if target is None:
            if len(candidates) > 1:
                lines = ["Which alert do you mean?\n"]
                for a in candidates:
                    lines.append(f"- #{a['id']}: {a['symbol']} — {a['condition']}")
                return "\n".join(lines)
            return "Couldn't find that alert. Try \"show my alerts\" to see the list."
        alert_remove(self._user_id, target["id"])
        return f"🗑️ Alert #{target['id']} on {target['symbol']} removed."

    def _pause_alert(self, msg: str) -> str:
        target, candidates = self._match_alert(msg)
        if target is None:
            return "Couldn't find that alert. Try \"show my alerts\" to see the list."
        alert_pause(self._user_id, target["id"])
        return f"⏸️ Alert #{target['id']} on {target['symbol']} paused."

    def _resume_alert(self, msg: str) -> str:
        symbol = extract_entity(msg)
        id_match = re.search(r"#?(\d+)", msg)
        alerts = alert_list(self._user_id)
        target = None
        if id_match:
            aid = int(id_match.group(1))
            target = next((a for a in alerts if a["id"] == aid), None)
        elif symbol:
            symbol = clean_ticker(symbol)
            matches = [a for a in alerts if a["symbol"] == symbol and not a["active"]]
            target = matches[0] if len(matches) == 1 else None
        if target is None:
            return "Couldn't find that alert. Try \"show my alerts\" to see the list."
        alert_resume(self._user_id, target["id"])
        return f"▶️ Alert #{target['id']} on {target['symbol']} resumed."

    # ── Standard agent contract ─────────────────────────────────────────────

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        return [
            "Show my alerts",
            "Alert me if RELIANCE drops below 2800",
            "Notify me on a MACD bullish crossover for INFY",
            "Cancel my TCS alert",
        ]

    def reset(self):
        self._client.reset()

    def load_history(self, messages: list[dict]):
        self._client.load_history(messages)
