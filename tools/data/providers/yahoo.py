"""Yahoo Finance via yfinance (unofficial): quotes, OHLCV, statements.

Wraps tools/data/fetcher.py, which already bounds every call, shares the
"yfinance" breaker and falls back BSE -> NSE bhavcopy for a quote (tagging
the result `_degraded_source`)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from tools.data import fetcher


class YFinanceProvider:
    name = "yfinance"

    def quote(self, symbol: str) -> dict[str, Any]:
        return fetcher.get_info(symbol)

    def ohlcv(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        return fetcher.get_ohlcv(symbol, period, interval)

    def financials(self, symbol: str) -> dict[str, Any]:
        return fetcher.get_financials(symbol)

    def corporate_actions(self, symbol: str) -> pd.DataFrame:
        raise NotImplementedError("yfinance corporate actions aren't wired; NSE provides them")

    def movers(self, direction: str = "gainers") -> pd.DataFrame:
        raise NotImplementedError("yfinance has no NSE movers list")

    def bulk_deals(self, symbol: str | None = None) -> pd.DataFrame:
        raise NotImplementedError("yfinance has no deal data")

    def block_deals(self, symbol: str | None = None) -> pd.DataFrame:
        raise NotImplementedError("yfinance has no deal data")
