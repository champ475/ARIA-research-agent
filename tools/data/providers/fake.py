"""Deterministic market data for end-to-end tests and offline demos
(`ARIA_FAKE_DATA=1`). Every value is derived from the symbol, so the same
question always gets the same numbers, and nothing touches the network."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import Any

import pandas as pd

from utils import faults
from utils.helpers import IST

# Fixed "as of" so answers render the same timestamp on every run.
FAKE_AS_OF = IST.localize(datetime(2026, 10, 15, 15, 30))
_MOVERS = ["TCS", "INFY", "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "ITC", "LT"]


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.upper().encode()).hexdigest()[:8], 16)


def _price(symbol: str) -> float:
    return round(100 + _seed(symbol) % 4900 + (_seed(symbol) % 100) / 100, 2)


class FakeProvider:
    name = "fake"

    def quote(self, symbol: str) -> dict[str, Any]:
        faults.data_call()
        s, price = _seed(symbol), _price(symbol)
        eps = round(price / (12 + s % 30), 2)
        return {
            "symbol": f"{symbol.upper()}.NS",
            "longName": f"{symbol.upper()} Limited",
            "shortName": symbol.upper(),
            "currency": "INR",
            "currentPrice": price,
            "regularMarketPrice": price,
            "previousClose": round(price * 0.99, 2),
            "open": round(price * 0.995, 2),
            "dayHigh": round(price * 1.01, 2),
            "dayLow": round(price * 0.985, 2),
            "fiftyTwoWeekHigh": round(price * 1.25, 2),
            "fiftyTwoWeekLow": round(price * 0.8, 2),
            "marketCap": int(price * (1e8 + s % 10**9)),
            "trailingPE": round(price / eps, 2),
            "priceToBook": round(1.5 + (s % 80) / 10, 2),
            "trailingEps": eps,
            "beta": round(0.6 + (s % 90) / 100, 2),
            "sector": "Technology" if s % 2 else "Financial Services",
            "industry": "Information Technology Services" if s % 2 else "Banks - Regional",
            "regularMarketTime": int(FAKE_AS_OF.timestamp()),
        }

    def ohlcv(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        faults.data_call()
        s, base = _seed(symbol), _price(symbol)
        days = pd.bdate_range(end=FAKE_AS_OF.date(), periods=250)
        closes = [base * (1 + 0.15 * math.sin(i / 20 + s % 7) - 0.0004 * (250 - i)) for i in range(250)]
        return pd.DataFrame(
            {
                "Open": [c * 0.995 for c in closes],
                "High": [c * 1.01 for c in closes],
                "Low": [c * 0.99 for c in closes],
                "Close": closes,
                "Volume": [1_000_000 + (s + i * 7919) % 500_000 for i in range(250)],
            },
            index=pd.DatetimeIndex(days, name="Date"),
        )

    def financials(self, symbol: str) -> dict[str, Any]:
        faults.data_call()
        s = _seed(symbol)
        revenue = 10_000e7 + (s % 90_000) * 1e7
        cols = [pd.Timestamp("2026-03-31"), pd.Timestamp("2025-03-31")]
        income = pd.DataFrame(
            {
                cols[0]: [revenue, revenue * 0.22, revenue * 0.16, revenue * 0.02],
                cols[1]: [revenue * 0.9, revenue * 0.2, revenue * 0.15, revenue * 0.02],
            },
            index=["Total Revenue", "EBIT", "Net Income", "Interest Expense"],
        )
        balance = pd.DataFrame(
            {
                cols[0]: [revenue * 0.8, revenue * 0.3, revenue * 1.6, revenue * 0.25],
                cols[1]: [revenue * 0.7, revenue * 0.3, revenue * 1.45, revenue * 0.22],
            },
            index=["Stockholders Equity", "Total Debt", "Total Assets", "Current Liabilities"],
        )
        empty = pd.DataFrame()
        return {
            "income_annual": income,
            "income_quarterly": empty,
            "balance_annual": balance,
            "balance_quarterly": empty,
            "cashflow_annual": empty,
            "cashflow_quarterly": empty,
        }

    def corporate_actions(self, symbol: str) -> pd.DataFrame:
        faults.data_call()
        return pd.DataFrame()

    def movers(self, direction: str = "gainers") -> pd.DataFrame:
        faults.data_call()
        sign = -1 if direction == "losers" else 1
        rows = [
            {"symbol": sym, "ltp": _price(sym), "pchange": round(sign * (4.5 - i * 0.5), 2), "volume": 1_000_000 + i}
            for i, sym in enumerate(_MOVERS[:5])
        ]
        return pd.DataFrame(rows)

    def bulk_deals(self, symbol: str | None = None) -> pd.DataFrame:
        faults.data_call()
        return pd.DataFrame()

    def block_deals(self, symbol: str | None = None) -> pd.DataFrame:
        faults.data_call()
        return pd.DataFrame()
