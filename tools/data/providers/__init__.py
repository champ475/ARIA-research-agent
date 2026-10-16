"""
tools/data/providers — one interface for market data (DATA-02).

Research tools ask `market_data()` for quotes, history, statements, corporate
actions, movers and deals instead of importing a scraper directly, so a
source can be swapped (a licensed vendor or a broker API such as Kite
Connect, Upstox or Dhan) by adding a provider class and changing a route,
not by editing every tool.

- `YFinanceProvider`: quote, OHLCV, financial statements (Yahoo, unofficial).
- `NseProvider`: movers, bulk/block deals, corporate actions (NSE's site API).
  Raises `SourceUnavailable("nse")` when NSE refuses or fails, so the answer
  can say "NSE data is temporarily unavailable" instead of showing nothing.
- `FakeProvider`: deterministic fixtures for end-to-end tests and demos
  (`ARIA_FAKE_DATA=1`); never touches the network.
- `RoutedProvider`: sends each method to the provider that owns it.

Per-source health lives in the circuit breakers (`utils/resilience.breaker_states`),
shown at /health/data. The licensing decision for a public launch is the
owner's (ARCHITECTURE.md, DATA-02).
"""

from __future__ import annotations

import threading
from typing import Any, Protocol

import pandas as pd


class MarketDataProvider(Protocol):
    name: str

    def quote(self, symbol: str) -> dict[str, Any]: ...

    def ohlcv(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame: ...

    def financials(self, symbol: str) -> dict[str, Any]: ...

    def corporate_actions(self, symbol: str) -> pd.DataFrame: ...

    def movers(self, direction: str = "gainers") -> pd.DataFrame: ...

    def bulk_deals(self, symbol: str | None = None) -> pd.DataFrame: ...

    def block_deals(self, symbol: str | None = None) -> pd.DataFrame: ...


METHODS = ("quote", "ohlcv", "financials", "corporate_actions", "movers", "bulk_deals", "block_deals")


class RoutedProvider:
    """Each method goes to the provider named in `routes` (method -> provider)."""

    name = "routed"

    def __init__(self, routes: dict[str, Any]):
        missing = set(METHODS) - set(routes)
        if missing:
            raise ValueError(f"no provider for: {', '.join(sorted(missing))}")
        self.routes = routes

    def source_for(self, method: str) -> str:
        return str(self.routes[method].name)

    def quote(self, symbol: str) -> dict[str, Any]:
        return dict(self.routes["quote"].quote(symbol))

    def ohlcv(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        return self.routes["ohlcv"].ohlcv(symbol, period, interval)

    def financials(self, symbol: str) -> dict[str, Any]:
        return dict(self.routes["financials"].financials(symbol))

    def corporate_actions(self, symbol: str) -> pd.DataFrame:
        return self.routes["corporate_actions"].corporate_actions(symbol)

    def movers(self, direction: str = "gainers") -> pd.DataFrame:
        return self.routes["movers"].movers(direction)

    def bulk_deals(self, symbol: str | None = None) -> pd.DataFrame:
        return self.routes["bulk_deals"].bulk_deals(symbol)

    def block_deals(self, symbol: str | None = None) -> pd.DataFrame:
        return self.routes["block_deals"].block_deals(symbol)


def default_provider() -> RoutedProvider:
    from tools.data.providers.nse import NseProvider
    from tools.data.providers.yahoo import YFinanceProvider

    yf, nse = YFinanceProvider(), NseProvider()
    return RoutedProvider(
        {
            "quote": yf,
            "ohlcv": yf,
            "financials": yf,
            "corporate_actions": nse,
            "movers": nse,
            "bulk_deals": nse,
            "block_deals": nse,
        }
    )


_active: Any = None
_lock = threading.Lock()


def market_data() -> Any:
    """The process's provider: FakeProvider under ARIA_FAKE_DATA, else the
    default yfinance + NSE routing."""
    global _active
    with _lock:
        if _active is None:
            from config import ARIA_FAKE_DATA

            if ARIA_FAKE_DATA:
                from tools.data.providers.fake import FakeProvider

                _active = FakeProvider()
            else:
                _active = default_provider()
        return _active


def set_provider(provider: Any) -> None:
    """Tests and tooling: install a provider (None = choose again on next use)."""
    global _active
    with _lock:
        _active = provider
