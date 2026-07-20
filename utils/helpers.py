"""utils/helpers.py — shared utilities, intent-first query parser, ticker resolver"""

import re
import math
import pytz
from datetime import datetime

IST = pytz.timezone("Asia/Kolkata")

# ── Alias map: common names → NSE ticker ──────────────────────────────────────
TICKER_ALIASES = {
    # Banks
    "SBI": "SBIN", "STATE BANK": "SBIN", "HDFC BANK": "HDFCBANK",
    "ICICI BANK": "ICICIBANK", "AXIS BANK": "AXISBANK",
    "KOTAK BANK": "KOTAKBANK", "KOTAK": "KOTAKBANK",
    "INDUSIND": "INDUSINDBK", "YES BANK": "YESBANK",
    "PUNJAB NATIONAL": "PNB", "CANARA": "CANBK", "UNION BANK": "UNIONBANK",
    # IT
    "HCL TECH": "HCLTECH", "HCL TECHNOLOGIES": "HCLTECH",
    "L&T INFOTECH": "LTIM", "MPHASIS": "MPHASIS",
    # Conglomerates
    "L&T": "LT", "LARSEN": "LT", "LARSEN TOUBRO": "LT",
    "TATA MOTORS": "TATAMOTORS", "TATA STEEL": "TATASTEEL",
    "TATA CONSUMER": "TATACONSUM", "TATA POWER": "TATAPOWER",
    "TATA COMMUNICATIONS": "TATACOMM",
    # Consumer
    "HUL": "HINDUNILVR", "HINDUSTAN UNILEVER": "HINDUNILVR",
    "NESTLE": "NESTLEIND", "ASIAN PAINTS": "ASIANPAINT",
    "BAJAJ AUTO": "BAJAJ-AUTO", "BAJAJ FINANCE": "BAJFINANCE",
    "BAJAJ FINSERV": "BAJAJFINSV",
    # Other
    "AIRTEL": "BHARTIARTL", "BHARTI": "BHARTIARTL",
    "HERO": "HEROMOTOCO", "HERO MOTOCORP": "HEROMOTOCO",
    "EICHER": "EICHERMOT", "MAHINDRA": "M&M",
    "COAL INDIA": "COALINDIA", "POWER GRID": "POWERGRID",
    "ADANI PORT": "ADANIPORTS", "ADANI PORTS": "ADANIPORTS",
    "SHREE CEMENT": "SHREECEM", "ULTRA CEMENT": "ULTRACEMCO",
    "ULTRA TECH": "ULTRACEMCO", "OIL AND GAS": "ONGC",
    # EV / New listings
    "ATHER": "ATHERENERG", "ATHER ENERGY": "ATHERENERG",
    "OLA ELECTRIC": "OLAELEC", "ZOMATO": "ZOMATO",
    "NYKAA": "NYKAA", "PAYTM": "PAYTM",
    "POLICYBAZAAR": "POLICYBZR", "DELHIVERY": "DELHIVERY",
    "IRCTC": "IRCTC", "HAL": "HAL", "BEL": "BEL",
}

# Words that are NEVER tickers — stop-list for regex extraction
STOP_WORDS = {
    "IC", "REPORT", "ON", "FOR", "THE", "AND", "OR", "VS", "AT",
    "CHART", "WEEKLY", "DAILY", "MONTHLY", "SHOW", "GET", "GIVE",
    "ANALYSE", "ANALYZE", "ANALYSIS", "RESEARCH", "TECHNICAL",
    "FUNDAMENTAL", "FUNDAMENTALS", "NEWS", "LATEST", "COMPARE", "BETWEEN",
    "SCREENER", "SCREEN", "FIND", "TOP", "BEST", "WORST",
    "QUICK", "FULL", "DETAILED", "BRIEF", "SHORT", "LONG",
    "NOTE", "UPDATE", "RESULT", "RESULTS", "EARNINGS", "SECTOR", "THEMATIC",
    "GENERATE", "WRITE", "CREATE", "MAKE", "PLEASE", "WHAT", "HOW",
    "INITIATING", "COVERAGE", "INITIATE", "FLASH", "MARKET",
    "OVERVIEW", "TODAY", "NIFTY", "SENSEX", "FII", "DII", "MACRO",
    "RSI", "MACD", "SMA", "EMA", "ADX", "ATR", "VWAP", "BB",
    "BACKTEST", "STRATEGY", "OPTION", "OPTIONS", "CALL", "PUT",
    "INTRADAY", "SWING", "POSITIONAL", "LONG", "SHORT", "BUY", "SELL",
    "PORTFOLIO", "HOLDING", "WATCHLIST", "ALERT", "PRICE", "CMP",
    "SUPPORT", "RESISTANCE", "LEVEL", "LEVELS", "ZONE", "ZONES",
    "BREAKOUT", "BREAKDOWN", "TARGET", "ENTRY", "EXIT", "PIVOT",
    "TREND", "MOMENTUM", "SIGNAL", "PATTERN", "SETUP", "TRIGGER",
    # Generic words mistaken for tickers
    "ANNUAL", "CHECK", "STOCK", "COMPANY", "DATA",
}


def now_ist() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")


def clean_ticker(raw: str) -> str:
    t = raw.strip().upper()
    for sfx in (".NS", ".BO", ".BSE", ".NSE"):
        if t.endswith(sfx):
            t = t[: -len(sfx)]
    return TICKER_ALIASES.get(t, t)


def fmt_num(val, prefix="₹", dec=2) -> str:
    if val is None:
        return "N/A"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "N/A"
    if math.isnan(v) or math.isinf(v):
        return "N/A"
    if abs(v) >= 1e7:
        return f"{prefix}{v/1e7:.{dec}f}Cr"
    if abs(v) >= 1e5:
        return f"{prefix}{v/1e5:.{dec}f}L"
    return f"{prefix}{v:,.{dec}f}"


def safe_float(val) -> float | None:
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def safe_val(val, fmt: str = "") -> str:
    """Return formatted value or 'N/A' — never None/nan in output."""
    f = safe_float(val)
    if f is None:
        return "N/A"
    if fmt == "pct":
        return f"{f*100:.2f}%"
    if fmt == "pct_direct":
        return f"{f:.2f}%"
    if fmt == "2f":
        return f"{f:.2f}"
    return str(round(f, 2))


def pct(val) -> str:
    f = safe_float(val)
    return "N/A" if f is None else f"{f*100:.2f}%"


def trim_text(text: str, max_words: int = 150) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " ...[truncated]"


# ── Intent-first query parser ─────────────────────────────────────────────────

# Ordered intent keywords — checked in order, first match wins
INTENT_MAP = [
    ("chart",       ["chart","candlestick","show price","ohlc","price chart","show chart"]),
    ("technical",   ["technical analysis","technical","rsi","macd","bollinger","supertrend",
                     "support resistance","indicator","ema","sma","adx","atr","vwap"]),
    ("fundamental", ["fundamental","valuation","pe ratio","roe","roce","dcf","financials",
                     "balance sheet","earnings","revenue","margins","eps","book value"]),
    ("options",     ["option chain","options chain","options","call put","open interest","oi","iv"]),
    ("backtest",    ["backtest","back test","strategy test","ma crossover","rsi strategy"]),
    ("news",        ["news","latest news","acquisition","merger","funding","deal","announced",
                     "corporate action","bulk deal","block deal"]),
    ("compare",     ["compare","vs ","versus"," vs"]),
    ("market",      ["market overview","market today","nifty","sensex","fii","dii","macro",
                     "market news","market update"]),
    ("screener",    ["screen","screener","find stocks","filter stocks","top gainer","top loser",
                     "top gainers","top losers","bulk deal","block deal","momentum stocks",
                     "value screen","quality screen"]),
    ("portfolio",   ["my portfolio","portfolio","my holdings","show holdings"]),
    ("port_add",    ["add to portfolio","bought","add tcs","add infy","add reliance"]),
    ("port_remove", ["remove from portfolio","sold","remove tcs"]),
    ("report_ic",   ["initiating coverage","ic report","initiate coverage","full report on",
                     "generate report","write report","create report","detailed report on"]),
    ("report_ru",   ["result update","earnings update","quarterly results","q1 results",
                     "q2 results","q3 results","q4 results"]),
    ("report_qn",   ["quick note","flash note","brief note","quick report"]),
    ("report_st",   ["sector report","thematic report","sector analysis","sector outlook",
                     "sector thematic"]),
    ("analyse",     ["analyse","analyze","analysis","research","deep dive","tell me about",
                     "should i buy","should i invest","full analysis","overview"]),
]


def parse_intent(query: str) -> str:
    """Return the detected intent string."""
    lower = query.lower().strip()
    for intent, keywords in INTENT_MAP:
        if any(kw in lower for kw in keywords):
            return intent
    return "chat"


def extract_entity(query: str) -> str | None:
    """
    Extract company name / ticker from query using intent-first approach.
    - Strips intent keywords first
    - Checks alias map
    - Falls back to yfinance search
    - NEVER returns a stop-word as a ticker
    """
    upper = query.upper().strip()

    # 1. Check alias map (longest match first)
    for alias, ticker in sorted(TICKER_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in upper:
            return ticker

    # 1b. "... for/on/of SYMBOL" pattern — highest-confidence preposition cue
    #     e.g. "annual report for HDFCBANK" → HDFCBANK
    #          "IC report on TCS"           → TCS
    prep = re.search(r'\b(?:FOR|ON|OF)\s+([A-Z][A-Z0-9\-&]{1,14})\b', upper)
    if prep:
        candidate = prep.group(1)
        if candidate not in STOP_WORDS and len(candidate) >= 2:
            return TICKER_ALIASES.get(candidate, candidate)

    # 2. Check explicit .NS / .BO suffix
    m = re.search(r'\b([A-Z][A-Z0-9\-&]{1,14})\.(NS|BO)\b', upper)
    if m:
        return m.group(1)

    # 3. Extract capitalised tokens, filter stop-words
    tokens = re.findall(r'\b([A-Z][A-Z0-9\-&]{1,14})\b', upper)
    for t in tokens:
        if t not in STOP_WORDS and len(t) >= 2:
            return TICKER_ALIASES.get(t, t)

    return None


def resolve_ticker(query: str) -> tuple[str | None, str]:
    """
    Full resolution pipeline:
    1. Alias map (exact phrase match)
    2. Explicit .NS/.BO suffix
    3. Token extraction (skip stop-words)
    4. yfinance Search API fallback

    Returns (ticker_symbol, source_description)
    """
    # 1 & 2 & 3
    candidate = extract_entity(query)
    if candidate:
        return clean_ticker(candidate), "alias/token extraction"

    # 4. Search API fallback
    try:
        import yfinance as yf
        results = yf.Search(query, max_results=5)
        for q in (results.quotes or []):
            if q.get("exchange", "") in ("NSI", "BSE", "NSE", "BOM"):
                sym = q.get("symbol", "").replace(".NS", "").replace(".BO", "")
                return clean_ticker(sym), "yfinance search API"
    except Exception:
        pass

    return None, "not found"
