"""
evals/answers/fixture_provider.py — pinned, realistic market data for the
answer eval (EVAL-01).

The 15 companies frozen in tests/fixtures/fundamentals/ answer with their real
Yahoo quote fields and statements (the reference facts the eval checks), and
the three in tests/fixtures/ohlcv/ with real bars. Everything else falls back
to FakeProvider, with fake bars rescaled to the real price where one is known.
Nothing touches the network.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import pandas as pd

from tools.data.providers.fake import FAKE_AS_OF, FakeProvider

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


@cache
def fixture(symbol: str) -> dict | None:
    path = FIXTURES / "fundamentals" / f"{symbol.upper()}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def symbols() -> list[str]:
    return sorted(p.stem for p in (FIXTURES / "fundamentals").glob("*.json"))


def _frame(rows: dict) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).T
    df.columns = [pd.Timestamp(c) for c in df.columns]
    return df[sorted(df.columns, reverse=True)]


class FixtureProvider(FakeProvider):
    name = "fixture"

    def quote(self, symbol: str) -> dict[str, Any]:
        base = super().quote(symbol)
        doc = fixture(symbol)
        if doc is None:
            return base
        info = dict(doc["info"])
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if price:
            # Day and 52-week ranges aren't frozen: keep the fake shape around the real price.
            scale = price / base["currentPrice"]
            for k in ("previousClose", "open", "dayHigh", "dayLow", "fiftyTwoWeekHigh", "fiftyTwoWeekLow"):
                base[k] = round(base[k] * scale, 2)
        base.update(info)
        base["shortName"] = info.get("longName", base["shortName"])
        base["regularMarketPrice"] = price or base["regularMarketPrice"]
        base["regularMarketTime"] = int(FAKE_AS_OF.timestamp())
        return base

    def ohlcv(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        path = FIXTURES / "ohlcv" / f"{symbol.upper()}.csv"
        if path.exists():
            df = pd.read_csv(path, index_col="Date", parse_dates=True)
            return df[df.index <= pd.Timestamp(FAKE_AS_OF.date())]
        df = super().ohlcv(symbol, period, interval)
        doc = fixture(symbol)
        price = doc and (doc["info"].get("currentPrice") or doc["info"].get("regularMarketPrice"))
        if price:
            scale = price / float(df["Close"].iloc[-1])
            for col in ("Open", "High", "Low", "Close"):
                df[col] = df[col] * scale
        return df

    def financials(self, symbol: str) -> dict[str, Any]:
        doc = fixture(symbol)
        if doc is None:
            return super().financials(symbol)
        empty = pd.DataFrame()
        return {
            "income_annual": _frame(doc.get("income_annual") or {}),
            "income_quarterly": empty,
            "balance_annual": _frame(doc.get("balance_annual") or {}),
            "balance_quarterly": empty,
            "cashflow_annual": _frame(doc.get("cashflow_annual") or {}),
            "cashflow_quarterly": empty,
        }
