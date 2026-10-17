"""
graph/alert_spec.py — what an alert means, validated before it is saved (VAL-04).

`parse_alert(message, symbol)` reads the condition deterministically for all
14 alert types and returns an `AlertSpec` (pydantic, with per-type range
rules) or an `AlertRefusal` saying what to fix. The threshold is the number
next to the comparator ("falls below 3,500 by Friday" -> 3500, not the last
number in the sentence), percent alerts carry a direction (a "drops 5%"
alert must not fire on a 5% rise), and Indian amounts ("3.5k", "1.2 lakh")
are understood. `AlertSpec.describe()` is the plain-words echo shown back
to the user, generated in code — never by the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from utils.numbers import AMOUNT_RE, amount_from_match, format_inr

AlertType = Literal[
    "price_above",
    "price_below",
    "pct_change",
    "rsi_cross_above",
    "rsi_cross_below",
    "macd_bullish_cross",
    "macd_bearish_cross",
    "ma_cross",
    "bb_breakout",
    "volume_spike",
    "week52_high",
    "week52_low",
    "bulk_deal",
    "portfolio_pnl",
]
ALERT_TYPES: tuple[str, ...] = AlertType.__args__  # type: ignore[attr-defined]
Direction = Literal["up", "down", "either"]

MAX_ACTIVE_ALERTS = 50
_NO_THRESHOLD = {
    "macd_bullish_cross",
    "macd_bearish_cross",
    "ma_cross",
    "bb_breakout",
    "week52_high",
    "week52_low",
    "bulk_deal",
}


class AlertSpec(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    alert_type: AlertType
    threshold: float | None = None
    direction: Direction | None = None
    recurring: bool = False
    cooldown_hours: int = Field(default=24, ge=1, le=168)

    @model_validator(mode="after")
    def _rules(self) -> AlertSpec:
        t, x = self.alert_type, self.threshold
        if t in _NO_THRESHOLD:
            self.threshold = None
        elif x is None:
            raise ValueError("needs a number to watch for")
        elif t in ("price_above", "price_below"):
            if not 0 < x < 10_000_000:
                raise ValueError("price must be a positive rupee amount")
        elif t == "pct_change":
            if not 0.1 <= x <= 50:
                raise ValueError("percent move must be between 0.1% and 50%")
        elif t in ("rsi_cross_above", "rsi_cross_below"):
            if not 0 < x < 100:
                raise ValueError("RSI runs from 0 to 100")
        elif t == "volume_spike":
            if not 1 < x <= 20:
                raise ValueError("volume spike must be between 1x and 20x average volume")
        elif t == "portfolio_pnl" and not 0.1 <= x <= 100:
            raise ValueError("portfolio P&L move must be between 0.1% and 100%")
        if t in ("pct_change", "portfolio_pnl"):
            self.direction = self.direction or "either"
        else:
            self.direction = None
        return self

    def describe(self) -> str:
        x = self.threshold if self.threshold is not None else 0.0
        move = {"up": "rises", "down": "falls", "either": "moves"}.get(self.direction or "", "moves")
        texts = {
            "price_above": lambda: f"price goes above {format_inr(x)}",
            "price_below": lambda: f"price falls below {format_inr(x)}",
            "pct_change": lambda: f"price {move} {x:g}% or more in a day",
            "rsi_cross_above": lambda: f"RSI(14) crosses above {x:g}",
            "rsi_cross_below": lambda: f"RSI(14) crosses below {x:g}",
            "macd_bullish_cross": lambda: "MACD crosses above its signal line (bullish)",
            "macd_bearish_cross": lambda: "MACD crosses below its signal line (bearish)",
            "ma_cross": lambda: "50-day average crosses the 200-day average (golden/death cross)",
            "bb_breakout": lambda: "price breaks out of its Bollinger Bands",
            "volume_spike": lambda: f"volume reaches {x:g}x its average",
            "week52_high": lambda: "price makes a new 52-week high",
            "week52_low": lambda: "price makes a new 52-week low",
            "bulk_deal": lambda: "a bulk/block deal is reported",
            "portfolio_pnl": lambda: f"your holding's P&L {move} {x:g}% or more",
        }
        return f"{self.symbol}: {texts[self.alert_type]()}" + (" (recurring)" if self.recurring else "")


@dataclass(frozen=True)
class AlertRefusal:
    reason: str


# ── deterministic parsing ─────────────────────────────────────────────────────

_NUM = AMOUNT_RE.pattern
_ABOVE = r"(?:goes?\s+|crosse?s?\s+|breaks?\s+|moves?\s+|rises?\s+|trades?\s+)?(?:above|over|beyond|past)|exceeds?|crosses|>=?"
_BELOW = r"(?:goes?\s+|drops?\s+|falls?\s+|dips?\s+|slips?\s+|breaks?\s+|trades?\s+|moves?\s+)?(?:below|under|beneath)|<=?"
_FILLER = r"\s*(?:the\s+)?(?:(?:level|mark|price)\s+(?:of\s+)?)?(?:of\s+)?"

_PRICE_ABOVE = re.compile(rf"(?:{_ABOVE}){_FILLER}(?P<amt>{_NUM})", re.IGNORECASE)
_PRICE_BELOW = re.compile(rf"(?:{_BELOW}){_FILLER}(?P<amt>{_NUM})", re.IGNORECASE)
_TOUCH = re.compile(rf"\b(?:hits?|touch(?:es)?|reach(?:es)?|at){_FILLER}(?P<amt>{_NUM})", re.IGNORECASE)

_DOWN = r"drops?|dropped|falls?|fell|declines?|down|dips?|crash(?:es)?|loses?|slides?|tanks?|plunges?|sinks?"
_UP = r"rises?|rose|gains?|jumps?|up|surges?|rall(?:y|ies)|climbs?|soars?"
_EITHER = r"moves?|changes?|swings?"
_PCT = r"(?P<pct>\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent|pc)"
_PCT_AFTER = re.compile(
    rf"\b(?P<dir>{_DOWN}|{_UP}|{_EITHER})\b(?:\s+(?:by|more\s+than|over))?\s*{_PCT}", re.IGNORECASE
)
_PCT_BEFORE = re.compile(
    rf"{_PCT}\s*(?P<dir>{_DOWN}|{_UP}|{_EITHER}|drop|fall|rise|gain|move|change)\b", re.IGNORECASE
)

_RSI = re.compile(
    r"\brsi\b(?:\s*\(?\s*14\s*\)?)?\D{0,25}?(?P<cmp>above|over|crosses\s+above|>|below|under|crosses\s+below|<)\s*(?P<n>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_VOLUME = re.compile(
    r"(?P<n>\d+(?:\.\d+)?)\s*(?:x|times)\s*(?:the\s+)?(?:avg|average|normal|usual)?\s*volume"
    r"|volume\D{0,25}?(?P<n2>\d+(?:\.\d+)?)\s*(?:x|times)",
    re.IGNORECASE,
)
_RECURRING = re.compile(
    r"\b(recurring|every\s*time|each\s*time|repeatedly|keep\s+(?:alerting|notifying)|every\s+day|daily\s+alert"
    r"|don'?t\s+deactivate|whenever)\b",
    re.IGNORECASE,
)


def _pct_direction(word: str) -> Direction:
    w = word.lower()
    if re.fullmatch(_DOWN + r"|drop|fall", w):
        return "down"
    if re.fullmatch(_UP + r"|rise|gain", w):
        return "up"
    return "either"


def _spec(symbol: str, msg: str, **fields: Any) -> AlertSpec | AlertRefusal:
    try:
        return AlertSpec(symbol=symbol, recurring=bool(_RECURRING.search(msg)), **fields)
    except ValidationError as e:
        reason = "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        return AlertRefusal(f"{fields.get('alert_type', 'alert').replace('_', ' ')}: {reason}")


def parse_alert(msg: str, symbol: str) -> AlertSpec | AlertRefusal | None:
    """AlertSpec, a refusal explaining what's wrong, or None when the message
    doesn't describe any alert type this parser recognises (the caller may
    then try the LLM extractor, whose output is validated the same way)."""
    text = msg or ""
    low = text.lower()

    if re.search(r"\b(bulk|block)\s+deals?\b", low):
        return _spec(symbol, text, alert_type="bulk_deal")
    if re.search(r"\b(portfolio|holdings?|position)\b", low) and (
        re.search(r"\b(p\s*&\s*l|pnl|profit|loss|returns?)\b", low) or re.search(_PCT, text, re.IGNORECASE)
    ):
        m = re.search(_PCT, text, re.IGNORECASE)
        if not m:
            return AlertRefusal("portfolio P&L alert: say the percent, e.g. 'if my TCS holding is down 10%'")
        direction: Direction = (
            "down"
            if re.search(r"\b(loss|down|falls?|drops?)\b", low)
            else ("up" if re.search(r"\b(profit|up|gains?|rises?)\b", low) else "either")
        )
        return _spec(
            symbol, text, alert_type="portfolio_pnl", threshold=float(m.group("pct")), direction=direction
        )
    m52 = re.search(r"\b52[\s-]*(?:week|wk|w)\s*(high|low)\b|\b(?:one|1)[\s-]*year\s*(high|low)\b", low)
    if m52:
        return _spec(symbol, text, alert_type="week52_high" if "high" in m52.group(0) else "week52_low")
    if re.search(
        r"\b(golden|death)\s+cross|\b(moving\s+average|dma|sma|ma)\s+cross|\b50\D{1,20}200\D{0,20}cross", low
    ):
        return _spec(symbol, text, alert_type="ma_cross")
    if "macd" in low:
        if re.search(r"bullish|crosses?\s+above|positive", low):
            return _spec(symbol, text, alert_type="macd_bullish_cross")
        if re.search(r"bearish|crosses?\s+below|negative", low):
            return _spec(symbol, text, alert_type="macd_bearish_cross")
        return AlertRefusal("MACD alert: say bullish (crosses above signal) or bearish (crosses below)")
    if re.search(r"\bbollinger|\bbb\s+breakout|\bband\s+breakout", low):
        return _spec(symbol, text, alert_type="bb_breakout")
    if re.search(r"\brsi\b", low):
        if "overbought" in low:
            return _spec(symbol, text, alert_type="rsi_cross_above", threshold=70.0)
        if "oversold" in low:
            return _spec(symbol, text, alert_type="rsi_cross_below", threshold=30.0)
        m = _RSI.search(text)
        if not m:
            return AlertRefusal("RSI alert: say above or below which level, e.g. 'RSI crosses below 30'")
        above = re.match(r"above|over|crosses\s+above|>", m.group("cmp"), re.IGNORECASE)
        return _spec(
            symbol,
            text,
            alert_type="rsi_cross_above" if above else "rsi_cross_below",
            threshold=float(m.group("n")),
        )
    if "volume" in low:
        m = _VOLUME.search(text)
        ratio = float(m.group("n") or m.group("n2")) if m else 2.0
        return _spec(symbol, text, alert_type="volume_spike", threshold=ratio)

    m = _PCT_AFTER.search(text) or _PCT_BEFORE.search(text)
    if m:
        return _spec(
            symbol,
            text,
            alert_type="pct_change",
            threshold=float(m.group("pct")),
            direction=_pct_direction(m.group("dir")),
        )

    above, below = _PRICE_ABOVE.search(text), _PRICE_BELOW.search(text)
    if above and below:
        return AlertRefusal("price alert: that mentions both above and below — set one alert per condition")
    for match, alert_type in ((above, "price_above"), (below, "price_below")):
        if match:
            amount = amount_from_match(AMOUNT_RE.search(match.group("amt")))  # type: ignore[arg-type]
            return _spec(symbol, text, alert_type=alert_type, threshold=amount)
    touch = _TOUCH.search(text)
    if touch:
        amount = amount_from_match(AMOUNT_RE.search(touch.group("amt")))  # type: ignore[arg-type]
        return AlertRefusal(
            f"price alert: should it fire when the price goes above or below {format_inr(amount or 0)}?"
        )
    return None


def spec_from_llm(data: dict, symbol: str, msg: str) -> AlertSpec | AlertRefusal | None:
    """Validate the LLM extractor's JSON exactly like a parsed message."""
    alert_type = data.get("alert_type")
    if alert_type not in ALERT_TYPES:
        return None
    threshold = data.get("threshold")
    direction = data.get("direction") if data.get("direction") in ("up", "down", "either") else None
    try:
        threshold = None if threshold is None else float(threshold)
    except (TypeError, ValueError):
        return AlertRefusal(f"{alert_type.replace('_', ' ')}: the number couldn't be read")
    return _spec(symbol, msg, alert_type=alert_type, threshold=threshold, direction=direction)


def already_true(spec: AlertSpec, price: float | None) -> bool:
    """A price alert whose condition already holds would fire on the next
    check — the user should confirm that's intended."""
    if price is None or spec.threshold is None:
        return False
    if spec.alert_type == "price_above":
        return price > spec.threshold
    if spec.alert_type == "price_below":
        return price < spec.threshold
    return False


def far_from_price(spec: AlertSpec, price: float | None) -> bool:
    """A price alert set so far from today's price that it is almost certainly a typo
    (TCS "below ₹1", or "above ₹3,00,000"): below 20% or above 3x the current price."""
    if price is None or price <= 0 or spec.threshold is None:
        return False
    if spec.alert_type == "price_below":
        return spec.threshold < 0.2 * price
    if spec.alert_type == "price_above":
        return spec.threshold > 3 * price
    return False


_CONFIRM = re.compile(
    r"\b(anyway|regardless|even\s+(?:if|though)|still\s+(?:set|create|add)|i\s+know)\b", re.IGNORECASE
)
_ALERT_ID = re.compile(r"(?:\balert\s*(?:#|no\.?|number|id)?\s*|#)(?P<id>\d{1,9})\b", re.IGNORECASE)


def confirms_anyway(msg: str) -> bool:
    return bool(_CONFIRM.search(msg or ""))


def alert_id_in(msg: str) -> int | None:
    m = _ALERT_ID.search(msg or "")
    return int(m.group("id")) if m else None
