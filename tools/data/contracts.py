"""
tools/data/contracts.py — what each external data source must send (TEST-05).

Upstreams (NSE, AMFI, mfapi, Yahoo, news feeds) change their responses
without notice; until now users found out. Each source's response is
described here as a pydantic model or a check function, holding only the
fields ARIA actually reads (extra fields are allowed, so additions upstream
don't break us; a renamed or missing field does). Used by:

- the recorded-response tests (tests/test_data_contracts.py) against the
  fixtures in tests/fixtures/http/;
- the live canary (scripts/data_canary.py, the worker's `data_canary` job),
  which pages the operator after two failures in a row (OBS-04);
- fetchers that validate in-line (NSE large deals).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ── NSE ───────────────────────────────────────────────────────────────────────


class NseMover(_Open):
    symbol: str
    ltp: float
    perChange: float


class _MoverBlock(_Open):
    data: list[NseMover]


class NseMovers(_Open):
    """GET /api/live-analysis-variations?index=gainers|loosers"""

    NIFTY: _MoverBlock


class NseDeal(_Open):
    symbol: str
    name: str
    clientName: str
    buySell: str
    qty: str | float
    watp: str | float
    date: str


class NseLargeDeals(_Open):
    """GET /api/snapshot-capital-market-largedeal (replaced bulk-/block-deal-data)."""

    as_on_date: str
    bulk: list[NseDeal] = Field(alias="BULK_DEALS_DATA")
    block: list[NseDeal] = Field(alias="BLOCK_DEALS_DATA")


class NseCorporateAction(_Open):
    """One row of GET /api/corporates-corporateActions?index=equities&symbol=X"""

    symbol: str
    subject: str
    exDate: str


BHAVCOPY_COLUMNS = {
    "SYMBOL",
    "SERIES",
    "DATE1",
    "PREV_CLOSE",
    "OPEN_PRICE",
    "HIGH_PRICE",
    "LOW_PRICE",
    "CLOSE_PRICE",
    "TTL_TRD_QNTY",
}


def check_bhavcopy(df: pd.DataFrame) -> None:
    """sec_bhavdata_full_<ddmmyyyy>.csv, after header whitespace is stripped."""
    missing = BHAVCOPY_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"bhavcopy columns missing: {sorted(missing)}")
    if df.empty or (df["SERIES"].str.strip() == "EQ").sum() == 0:
        raise ValueError("bhavcopy has no EQ rows")
    float(df["CLOSE_PRICE"].iloc[0])


# ── mutual funds ──────────────────────────────────────────────────────────────

NAV_COLUMNS = {"scheme_code", "scheme_name", "amc", "category", "plan", "option", "nav", "nav_date"}


def check_navall(df: pd.DataFrame) -> None:
    """AMFI NAVAll.txt after tools/data/mf_data.parse_navall."""
    missing = NAV_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"NAVAll columns missing: {sorted(missing)}")
    if len(df) < 10 or not (df["nav"] > 0).all():
        raise ValueError("NAVAll parsed to too few rows or non-positive NAVs")


class _MfapiMeta(_Open):
    scheme_code: int
    scheme_name: str


class _MfapiPoint(_Open):
    date: str
    nav: str

    @field_validator("nav")
    @classmethod
    def _positive(cls, v: str) -> str:
        if float(v) <= 0:
            raise ValueError("NAV must be positive")
        return v


class MfapiHistory(_Open):
    """GET https://api.mfapi.in/mf/<code>"""

    meta: _MfapiMeta
    data: list[_MfapiPoint] = Field(min_length=1)


# ── news ──────────────────────────────────────────────────────────────────────


def check_feed(feed: Any) -> None:
    """A feedparser result: at least one entry with a title and a link."""
    entries = getattr(feed, "entries", None) or []
    good = [e for e in entries if e.get("title") and e.get("link")]
    if not good:
        raise ValueError("feed has no entries with a title and link")


# ── BSE ───────────────────────────────────────────────────────────────────────


class _BseFiling(_Open):
    SubmissionDate: str
    NewsType: str
    AttachmentName: str


class BseFilings(_Open):
    """GET api.bseindia.com/BseIndiaAPI/api/AnnualReport/w?scripcode=X&type=Quarterly"""

    Table: list[_BseFiling]


# ── Yahoo (via yfinance) ──────────────────────────────────────────────────────


def check_quote(info: dict) -> None:
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if not isinstance(price, (int, float)) or price <= 0:
        raise ValueError("quote has no positive currentPrice/regularMarketPrice")


def check_bars(df: pd.DataFrame) -> None:
    missing = {"Open", "High", "Low", "Close", "Volume"} - set(df.columns)
    if missing:
        raise ValueError(f"OHLCV columns missing: {sorted(missing)}")
    if len(df) < 5 or df["Close"].isna().all():
        raise ValueError("OHLCV has too few bars")
