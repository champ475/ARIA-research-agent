"""NSE's website API: movers, bulk/block deals, corporate actions.

Uses the strict fetchers in tools/data/nse_scraper.py: a blocked or failing
NSE raises SourceUnavailable("nse") (or CircuitOpen while the "nse" breaker
is open) instead of returning an empty table that reads as "no data"."""

from __future__ import annotations

from typing import Any

import pandas as pd

from tools.data import nse_scraper


class NseProvider:
    name = "nse"

    def quote(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError("live NSE quotes aren't wired; yfinance provides them")

    def ohlcv(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        raise NotImplementedError("NSE history isn't wired; yfinance provides it")

    def financials(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError("NSE statements aren't wired; yfinance provides them")

    def corporate_actions(self, symbol: str) -> pd.DataFrame:
        return nse_scraper.fetch_corporate_actions(symbol)

    def movers(self, direction: str = "gainers") -> pd.DataFrame:
        return nse_scraper.fetch_top_movers(direction)

    def bulk_deals(self, symbol: str | None = None) -> pd.DataFrame:
        return nse_scraper.fetch_bulk_deals(symbol)

    def block_deals(self, symbol: str | None = None) -> pd.DataFrame:
        return nse_scraper.fetch_block_deals(symbol)
