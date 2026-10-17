"""
graph/quote_fields.py — the quote fields worth showing the model (LLM-04).

yfinance's `info` dict carries ~150 fields (maxAge, gmtOffSetMilliseconds,
fullTimeEmployees, ...). Turning each into a conclusion bloated the synthesis
prompt, crowded out the signal and invited the model to cite trivia. Only
these fields reach the prompt, labelled and in human units (₹, ₹ crore, %).
The full dict is still used by the tools that compute from it.
"""

from __future__ import annotations

from typing import Any

# yfinance field -> (label, unit, multiplier)
QUOTE_FIELDS: dict[str, tuple[str, str, float]] = {
    "currentPrice": ("Price", "₹", 1.0),
    "previousClose": ("Previous close", "₹", 1.0),
    "open": ("Open", "₹", 1.0),
    "dayHigh": ("Day high", "₹", 1.0),
    "dayLow": ("Day low", "₹", 1.0),
    "fiftyTwoWeekHigh": ("52-week high", "₹", 1.0),
    "fiftyTwoWeekLow": ("52-week low", "₹", 1.0),
    "marketCap": ("Market cap", "₹ crore", 1e-7),
    "trailingPE": ("P/E (trailing)", "", 1.0),
    "forwardPE": ("P/E (forward)", "", 1.0),
    "priceToBook": ("P/B", "", 1.0),
    "trailingEps": ("EPS (TTM)", "₹", 1.0),
    "bookValue": ("Book value per share", "₹", 1.0),
    "trailingAnnualDividendYield": ("Dividend yield", "%", 100.0),
    "beta": ("Beta", "", 1.0),
    "returnOnEquity": ("ROE (reported)", "%", 100.0),
    "profitMargins": ("Net margin", "%", 100.0),
    "revenueGrowth": ("Revenue growth (YoY)", "%", 100.0),
    "earningsGrowth": ("Earnings growth (YoY)", "%", 100.0),
    "volume": ("Volume", "shares", 1.0),
    "averageVolume": ("Average volume", "shares", 1.0),
    "targetMeanPrice": ("Analyst mean target", "₹", 1.0),
}


def _usable(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v  # excludes NaN


def quote_rows(payload: Any) -> list[tuple[str, str, float, str]]:
    """(field, label, value in display units, unit) for each allowlisted
    field present, in QUOTE_FIELDS order. `currentPrice` falls back to
    `regularMarketPrice`."""
    if not isinstance(payload, dict):
        return []
    data = dict(payload)
    if not _usable(data.get("currentPrice")) and _usable(data.get("regularMarketPrice")):
        data["currentPrice"] = data["regularMarketPrice"]
    rows = []
    for field, (label, unit, mult) in QUOTE_FIELDS.items():
        v = data.get(field)
        if isinstance(v, (int, float)) and _usable(v):
            value = round(float(v) * mult, 2)
            rows.append((field, label, value, unit))
    return rows


def display_value(value: float, unit: str) -> str:
    """₹3,512.40 · ₹12,73,000 crore → '₹1,273,000.00 crore' · 18.5% · 1.12."""
    if unit == "₹":
        return f"₹{value:,.2f}"
    if unit == "₹ crore":
        return f"₹{value:,.2f} crore"
    if unit == "%":
        return f"{value}%"
    if unit == "shares":
        return f"{value:,.0f} shares"
    return f"{value}"


def quote_summary(payload: Any) -> dict[str, float]:
    """{label: value} for prompts that summarise evidence."""
    return {f"{label} ({unit})" if unit else label: value for _f, label, value, unit in quote_rows(payload)}
