"""
tools/data/instruments.py — the instrument master and name resolution (VAL-02).

Built from vendored snapshots (refresh with scripts/refresh_instruments.py):
- data/instruments/nse_equity_l.csv — NSE EQUITY_L.csv (symbol, company
  name, series, ISIN);
- data/instruments/bse_codes.csv — BSE scrip code -> ISIN (from BSE's daily
  bhavcopy), so "500325" resolves to RELIANCE through the shared ISIN.

`resolve(text)` turns one company mention into a typed Resolution:
exact symbol -> renamed symbol -> BSE code -> alias -> company name (an
exact name that is also the prefix of others, or a bare group name like
"HDFC", is *ambiguous*) -> fuzzy match on names and symbols (rapidfuzz):
score >= 90 with a clear margin auto-resolves, 75-90 asks "did you mean",
below that is not_found with suggestions. It never invents a symbol: every
answer is a row of the master.
"""

from __future__ import annotations

import csv
import logging
import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from rapidfuzz import fuzz

log = logging.getLogger(__name__)

_DATA = Path(__file__).resolve().parent.parent.parent / "data" / "instruments"
NSE_FILE = _DATA / "nse_equity_l.csv"
BSE_FILE = _DATA / "bse_codes.csv"

AUTO_SCORE = 90.0
ASK_SCORE = 75.0
SUGGEST_SCORE = 60.0
_MARGIN = 5.0
_MAX_CANDIDATES = 5

# Symbols that changed. Old symbol -> (current symbol, note shown to the user).
RENAMED: dict[str, tuple[str, str]] = {
    "ZOMATO": ("ETERNAL", "Zomato Ltd is now listed as Eternal Ltd (ETERNAL)."),
    "LTIM": ("LTM", "LTIMindtree is now listed as LTM Ltd (LTM)."),
    "LTIMINDTREE": ("LTM", "LTIMindtree is now listed as LTM Ltd (LTM)."),
    "MINDTREE": ("LTM", "Mindtree merged into LTIMindtree, now listed as LTM Ltd (LTM)."),
    "TATAMOTORS": (
        "TMPV",
        "After the demerger, the old TATAMOTORS listing is TMPV (Tata Motors Passenger Vehicles); "
        "the commercial-vehicle business trades as TMCV.",
    ),
}

# Exact company names that still need a clarifying question: "Tata Motors"
# is now both TMCV (named "Tata Motors Limited") and TMPV (the old TATAMOTORS
# listing, renamed Tata Motors Passenger Vehicles) — people mean either.
AMBIGUOUS_EXACT_NAMES = frozenset({"tata motors"})

# Words that are never a company mention on their own: indices, market and
# fund jargon, ratios. Used by the query-level mention extractor
# (graph/entity_resolution.py) so "best ELSS funds" or "PE of TCS" don't
# produce a bogus "couldn't find ELSS".
NON_COMPANY_TERMS = frozenset(
    """
    NIFTY NIFTY50 SENSEX BANKNIFTY FINNIFTY MIDCAP SMALLCAP LARGECAP INDEX INDICES
    ELSS SIP SWP STP NAV MF MFS ETF ETFS IPO IPOS FPO NFO AMC AUM TER XIRR CAGR IDCW
    PE PB PS EPS ROE ROCE ROA DE EV EBITDA PAT OPM NPM CMP LTP ATH YOY QOQ TTM FY
    RSI MACD SMA EMA VWAP OBV ADX ATR BB DMA
    NSE BSE SEBI RBI AMFI NPS PPF EPF FD RD EMI GDP CPI WPI INR USD FII DII FPI
    LTCG STCG TDS GST KYC PAN DEMAT
    FMCG PSU IT BFSI OMC NBFC
    OK HI HELLO THANKS AI LLM PDF CSV
    BUY SELL HOLD SHOULD WHAT WHY HOW WHEN WHICH
    VALUE QUALITY GROWTH MOMENTUM DIVIDEND DEFENSIVE CYCLICAL ENERGY BANKING BANKS PHARMA AUTO
    METAL METALS TECH INFRA REALTY SMALL MID LARGE CAP CAPS PENNY BLUECHIP CHEAP TOP BEST GOOD
    """.split()
)

_LEGAL_SUFFIX = re.compile(r"\b(limited|ltd|the)\b")

# Words too common in company names to tell two companies apart — dropped
# before fuzzy matching, or "Stark Industries" scores 88 against any
# "<X> Industries".
_GENERIC_WORDS = frozenset(
    """industries industry corporation corp company co holdings holding enterprises enterprise
    laboratories laboratory labs technologies technology services systems solutions international
    global products ventures group and of india""".split()
)


def _is_generic(word: str) -> bool:
    # Typos of generic words ("industires", "servics", "indya") are generic too.
    return word in _GENERIC_WORDS or (len(word) >= 4 and any(fuzz.ratio(word, g) >= 80 for g in _GENERIC_WORDS))


def _distinctive(norm: str) -> str:
    words = [w for w in norm.split() if not _is_generic(w)]
    return " ".join(words) if words else norm
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def normalise_name(text: str) -> str:
    t = (text or "").lower().replace("&", " and ")
    t = _NON_ALNUM.sub(" ", t)
    t = _LEGAL_SUFFIX.sub(" ", t)
    return " ".join(t.split())


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    isin: str
    series: str
    norm: str  # normalise_name(name)
    distinct: str = ""  # _distinctive(norm), for fuzzy matching


@dataclass(frozen=True)
class Candidate:
    symbol: str
    name: str
    score: float


@dataclass(frozen=True)
class Resolution:
    query: str
    status: Literal["found", "ambiguous", "not_found"]
    symbol: str | None = None
    name: str | None = None
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)
    note: str | None = None
    method: str = ""

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "status": self.status,
            "symbol": self.symbol,
            "name": self.name,
            "candidates": [{"symbol": c.symbol, "name": c.name, "score": round(c.score, 1)} for c in self.candidates],
            "note": self.note,
        }


class _Master:
    def __init__(self) -> None:
        self.by_symbol: dict[str, Instrument] = {}
        self.by_isin: dict[str, Instrument] = {}
        self.by_norm: dict[str, list[Instrument]] = {}
        self.bse_to_isin: dict[str, str] = {}
        self.bse_names: dict[str, str] = {}
        if not NSE_FILE.exists():
            # Degrade, don't break every turn: resolve against the curated
            # universe only (exact symbols and aliases still work).
            log.error(f"[instruments] {NSE_FILE} is missing — resolving against the curated universe only")
            from config import STOCK_UNIVERSE

            for sym in STOCK_UNIVERSE:
                inst = Instrument(symbol=sym, name=sym, isin="", series="EQ", norm=normalise_name(sym))
                self.by_symbol[sym] = inst
                self.by_norm.setdefault(inst.norm, []).append(inst)
            self.instruments = list(self.by_symbol.values())
            return
        with open(NSE_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                row = {k.strip(): (v or "").strip() for k, v in row.items()}
                inst = Instrument(
                    symbol=row["SYMBOL"],
                    name=row["NAME OF COMPANY"],
                    isin=row["ISIN NUMBER"],
                    series=row["SERIES"],
                    norm=normalise_name(row["NAME OF COMPANY"]),
                    distinct=_distinctive(normalise_name(row["NAME OF COMPANY"])),
                )
                self.by_symbol[inst.symbol] = inst
                self.by_isin[inst.isin] = inst
                self.by_norm.setdefault(inst.norm, []).append(inst)
        if BSE_FILE.exists():
            with open(BSE_FILE, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    self.bse_to_isin[row["bse_code"]] = row["isin"]
                    self.bse_names[row["bse_code"]] = row["name"]
        self.instruments = list(self.by_symbol.values())


_master: _Master | None = None
_master_lock = threading.Lock()


def master() -> _Master:
    global _master
    if _master is None:
        with _master_lock:
            if _master is None:
                _master = _Master()
    return _master


def is_listed_symbol(symbol: str) -> bool:
    return (symbol or "").upper() in master().by_symbol


def _curated() -> set[str]:
    from config import STOCK_UNIVERSE

    return set(STOCK_UNIVERSE)


def _rank(cands: list[Candidate]) -> list[Candidate]:
    """Best score first; on ties prefer the curated universe, then EQ series."""
    curated = _curated()
    m = master()

    def key(c: Candidate) -> tuple:
        inst = m.by_symbol.get(c.symbol)
        return (-c.score, c.symbol not in curated, (inst.series if inst else "") != "EQ", c.symbol)

    out: list[Candidate] = []
    seen: set[str] = set()
    for c in sorted(cands, key=key):
        if c.symbol not in seen:
            seen.add(c.symbol)
            out.append(c)
    return out


def _found(query: str, inst: Instrument, method: str, note: str | None = None) -> Resolution:
    return Resolution(query, "found", inst.symbol, inst.name, note=note, method=method)


def _ambiguous(query: str, cands: list[Candidate], method: str, note: str | None = None) -> Resolution:
    return Resolution(query, "ambiguous", candidates=tuple(_rank(cands)[:_MAX_CANDIDATES]), note=note, method=method)


def _aliases() -> dict[str, str]:
    from utils.helpers import TICKER_ALIASES

    return {normalise_name(k): v for k, v in TICKER_ALIASES.items()}


@lru_cache(maxsize=4096)
def resolve(text: str) -> Resolution:
    query = (text or "").strip()
    m = master()
    token = re.sub(r"\.(NS|BO)$", "", query.upper().strip())
    norm = normalise_name(query)
    if not norm:
        return Resolution(query, "not_found", method="empty")

    # 1. Exact symbol (any series).
    if token in m.by_symbol:
        return _found(query, m.by_symbol[token], "symbol")

    # 2. A symbol that has since changed.
    if token in RENAMED:
        new_symbol, note = RENAMED[token]
        if new_symbol in m.by_symbol:
            return _found(query, m.by_symbol[new_symbol], "renamed", note)

    # 3. BSE scrip code -> ISIN -> NSE listing.
    if re.fullmatch(r"\d{6}", token):
        isin = m.bse_to_isin.get(token)
        inst = m.by_isin.get(isin or "")
        if inst:
            return _found(query, inst, "bse_code")
        if isin:
            return Resolution(
                query,
                "not_found",
                note=f"BSE code {token} ({m.bse_names.get(token, 'unknown')}) isn't listed on NSE, "
                "which ARIA's data covers.",
                method="bse_only",
            )
        return Resolution(query, "not_found", note=f"No company has BSE code {token}.", method="bse_code")

    # 4. Curated aliases ("SBI" -> SBIN, "HUL" -> HINDUNILVR).
    alias = _aliases().get(norm)
    if alias and alias in m.by_symbol:
        return _found(query, m.by_symbol[alias], "alias")

    # 5. Company name. An exact full name wins ("mahindra and mahindra" over
    #    "... financial services") unless it is a known ambiguous one; a bare
    #    group name that only starts several names ("hdfc", "bajaj") asks.
    starts = [i for i in m.instruments if i.norm == norm or i.norm.startswith(norm + " ")]
    exact = [i for i in starts if i.norm == norm]
    if len(exact) == 1 and norm not in AMBIGUOUS_EXACT_NAMES:
        return _found(query, exact[0], "name")
    if len(starts) == 1:
        return _found(query, starts[0], "name")
    if len(starts) > 1:
        cands = [Candidate(i.symbol, i.name, 100.0 if i in exact else 95.0) for i in starts]
        return _ambiguous(query, cands, "name_prefix")

    # 6. Fuzzy. A name score averages two views — distinctive words only
    #    (so a shared "Industries" can't make "Stark Industries" look like
    #    "Star Cement") and the full name (so "relaince industries" still beats
    #    "Reliance Power") — each against the same number of leading words of
    #    the candidate. A single typed token of 5+ characters is also compared
    #    with the symbol (a mistyped ticker); shorter symbol fuzzing is noise.
    distinct = _distinctive(norm)
    n_distinct = len(distinct.split())
    n_full = len(norm.split())
    symbol_like = " " not in query.strip() and len(token) >= 5
    scored: list[Candidate] = []
    for inst in m.instruments:
        d_score = fuzz.ratio(distinct, " ".join(inst.distinct.split()[:n_distinct]))
        if d_score < SUGGEST_SCORE and not symbol_like:
            continue
        f_score = fuzz.ratio(norm, " ".join(inst.norm.split()[:n_full]))
        score = max((d_score + f_score) / 2, fuzz.ratio(token, inst.symbol) if symbol_like else 0.0)
        if score >= SUGGEST_SCORE:
            scored.append(Candidate(inst.symbol, inst.name, float(score)))
    ranked = _rank(scored)
    if not ranked or ranked[0].score < ASK_SCORE:
        return Resolution(query, "not_found", candidates=tuple(ranked[:3]), method="fuzzy")
    top = ranked[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    if top.score >= AUTO_SCORE and top.score - runner_up >= _MARGIN:
        return _found(query, m.by_symbol[top.symbol], "fuzzy")
    return _ambiguous(query, [c for c in ranked if c.score >= ASK_SCORE], "fuzzy")


def reset_cache() -> None:
    """Tests / refresh: drop the loaded master and memoised resolutions."""
    global _master
    with _master_lock:
        _master = None
    resolve.cache_clear()
