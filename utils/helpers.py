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
    # Modal verbs / question words / generic connectors — closes the
    # "what price should I buy TCS at" class of bug at the deterministic
    # layer, before ever needing an LLM call.
    "SHOULD", "WOULD", "COULD", "CAN", "WILL", "MUST", "MAY", "MIGHT", "SHALL", "OR",
    "WITH", "FROM", "INTO", "ABOUT", "THIS", "THAT", "THESE", "THOSE",
    "WHEN", "WHERE", "WHICH", "WHO", "WHY", "WHOM",
    "DOES", "DO", "DID", "IS", "ARE", "WAS", "WERE", "BE", "BEEN", "BEING",
    "HAVE", "HAS", "HAD", "NOT", "NOW", "THEN", "THAN",
    "YOU", "YOUR", "YOURS", "MY", "MINE", "ME", "WE", "OUR", "THEY", "THEIR", "IT", "ITS",
    "SOME", "ANY", "ALL", "EACH", "EVERY", "MUCH", "MANY", "MORE", "MOST",
    "GOOD", "BAD", "WORTH", "WORTHY", "SAFE", "SAFELY", "SOON", "RIGHT", "STILL",
    "INVEST", "INVESTING", "INVESTMENT", "HOLD", "HOLDING",
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


def extract_all_entities(query: str) -> list[str]:
    """
    Find every distinct company/ticker mentioned in query, in order of first
    appearance, deduped. Unlike extract_entity() (which returns on the FIRST
    alias/token match and silently drops everything else), this is used to
    detect multi-ticker queries like "should I buy TCS or HDFC bank" that
    would otherwise collapse to whichever alias happens to match first.
    """
    upper = query.upper()
    masked = list(upper)
    matches: list[tuple[int, str]] = []

    # 1. Alias map, longest alias first, so "HDFC BANK" is consumed before
    #    a bare "HDFC" or "BANK" token could be picked up in step 3.
    for alias, ticker in sorted(TICKER_ALIASES.items(), key=lambda x: -len(x[0])):
        text = "".join(masked)
        idx = text.find(alias)
        while idx != -1:
            matches.append((idx, ticker))
            for i in range(idx, idx + len(alias)):
                masked[i] = " "
            text = "".join(masked)
            idx = text.find(alias)

    # 2. Explicit .NS/.BO suffix on whatever text is left unmasked.
    remaining = "".join(masked)
    for m in re.finditer(r'\b([A-Z][A-Z0-9\-&]{1,14})\.(NS|BO)\b', remaining):
        matches.append((m.start(), m.group(1)))
        for i in range(m.start(), m.end()):
            masked[i] = " "

    # 3. Capitalised tokens not in stopwords, on remaining unmasked text.
    remaining = "".join(masked)
    for m in re.finditer(r'\b([A-Z][A-Z0-9\-&]{1,14})\b', remaining):
        t = m.group(1)
        if t not in STOP_WORDS and len(t) >= 2:
            matches.append((m.start(), t))

    matches.sort(key=lambda x: x[0])
    seen: set[str] = set()
    result: list[str] = []
    for _, tk in matches:
        tk = clean_ticker(tk)
        if tk not in seen:
            seen.add(tk)
            result.append(tk)
    return result


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


# ── Hybrid resolver: deterministic-first, LLM fallback on ambiguity ────────────

_VALID_LLM_INTENTS = None  # lazily computed


def _known_intents() -> set[str]:
    """All intent labels the deterministic INTENT_MAP can produce, plus 'chat'."""
    return {intent for intent, _ in INTENT_MAP} | {"chat"}


def is_known_ticker(ticker: str) -> bool:
    """Cheap, no-network membership check against the curated sector universe."""
    from config import NIFTY200
    return ticker in NIFTY200


def ticker_has_market_data(ticker: str) -> bool:
    """Network check via yfinance. Mirrors the predicate used in equity_agent.py::_full_analysis."""
    try:
        from tools.data.fetcher import get_info
        info = get_info(ticker)
        return bool(info) and (
            info.get("regularMarketPrice") is not None
            or info.get("currentPrice") is not None
        )
    except Exception:
        return False


def validate_ticker(ticker: str | None) -> bool:
    """
    True only if `ticker` is confidently real. Used both to decide whether the
    LLM fallback is needed, and to sanity-check whatever the LLM returns before
    it is trusted downstream. Curated-universe membership avoids a network call
    for the common case; anything else falls through to a live yfinance check.
    """
    if not ticker:
        return False
    return is_known_ticker(ticker) or ticker_has_market_data(ticker)


def llm_resolve_query(query: str, client) -> dict:
    """
    LLM fallback for ticker + intent, used ONLY when the deterministic pass in
    resolve_query() is ambiguous. `client` is a caller-owned
    agents.llm_client.LLMClient instance — reuses existing Groq key-rotation /
    Gemini-fallback machinery, no new LLM wrapper is created.

    Never raises — returns {"ticker": None, "intent": "chat"} on any failure
    (network, parse, or malformed-JSON error), so callers never need a try/except.
    """
    import json
    from config import NIFTY200, TOKENS_FAST

    global _VALID_LLM_INTENTS
    if _VALID_LLM_INTENTS is None:
        _VALID_LLM_INTENTS = _known_intents()

    universe = ", ".join(NIFTY200)
    prompt = (
        "You are a strict entity/intent extractor for an Indian stock-market chatbot. "
        "Do not explain, do not add commentary — output ONLY a single JSON object.\n\n"
        f"Known NSE tickers (prefer these if the query matches one): {universe}\n\n"
        f'User query: "{query}"\n\n'
        "Extract:\n"
        "1. \"ticker\": the single NSE ticker symbol the query is about (uppercase, no "
        "suffix, e.g. \"TCS\" not \"TCS.NS\"). If the query names a company not in the "
        "known list, still give your best-guess correct NSE symbol. If no company or "
        "stock is mentioned at all, use null.\n"
        "2. \"intent\": exactly one of "
        "[\"chart\",\"technical\",\"fundamental\",\"news\",\"options\",\"backtest\","
        "\"compare\",\"market\",\"screener\",\"analyse\",\"chat\"]. "
        "Use \"analyse\" for general research/opinion/should-I-buy questions.\n\n"
        "Respond with ONLY this JSON shape, nothing else, no markdown fences:\n"
        '{"ticker": "SYMBOL_OR_null", "intent": "one_of_the_list_above"}'
    )

    print(f"[LLM RESOLVE] prompt sent to LLM:\n{prompt}\n[LLM RESOLVE] --- end prompt ---")

    try:
        raw = client.ask(prompt, temperature=0.0, max_tokens=TOKENS_FAST)
        print(f"[LLM RESOLVE] raw response: {raw!r}")
        raw = raw.strip().strip("```json").strip("```").strip()
        data = json.loads(raw)

        raw_ticker = data.get("ticker")
        ticker = None
        if isinstance(raw_ticker, str):
            candidate = raw_ticker.strip().upper()
            if candidate not in ("", "NULL", "NONE", "N/A"):
                ticker = clean_ticker(candidate)

        intent = data.get("intent")
        if intent not in _VALID_LLM_INTENTS:
            intent = "chat"

        print(f"[LLM RESOLVE] parsed: ticker={ticker!r} intent={intent!r}")
        return {"ticker": ticker, "intent": intent}
    except Exception as e:
        print(f"[LLM RESOLVE] failed to parse LLM output: {e!r}")
        return {"ticker": None, "intent": "chat"}


def resolve_query(query: str, client=None, last_symbol: str | None = None) -> dict:
    """
    Deterministic-first, LLM-fallback-on-ambiguity resolver for ticker + intent.

    Returns {"ticker": str|None, "intent": str, "source": str, "used_llm": bool}.

    Pipeline:
    1. Deterministic pass (free, fast): resolve_ticker() + parse_intent().
    2. Validate the deterministic ticker with validate_ticker().
    3. Trust the deterministic result outright (no LLM call) when a valid ticker
       was found AND the intent is not the generic "chat" bucket — the fast path
       for clean queries like "chart TCS", "analyse RELIANCE".
    4. Otherwise, only if `client` is provided, fall back to ONE combined LLM
       call (llm_resolve_query) when:
         - deterministic ticker is None, OR
         - deterministic ticker failed validate_ticker() (bogus token), OR
         - a ticker WAS found but intent == "chat" (keyword list matched nothing).
    5. The LLM's ticker is NEVER trusted directly — it is re-validated with
       validate_ticker(). If it also fails, fall back to the deterministic
       ticker (if valid) or `last_symbol` (session continuity) or None.
    """
    det_ticker, det_source = resolve_ticker(query)
    det_intent = parse_intent(query)
    det_ok = validate_ticker(det_ticker)

    print(f"[PARSE] query={query!r}")
    print(f"[PARSE] deterministic: ticker={det_ticker!r} valid={det_ok} intent={det_intent!r} source={det_source!r}")

    needs_llm = (det_ticker is None) or (not det_ok) or (det_ticker is not None and det_intent == "chat")

    if not needs_llm or client is None:
        result = {
            "ticker": det_ticker if det_ok else None,
            "intent": det_intent,
            "source": det_source,
            "used_llm": False,
        }
        print(f"[PARSE] final (no LLM call): {result}")
        return result

    print(f"[PARSE] ambiguous -> falling back to LLM "
          f"(det_ticker={det_ticker!r}, det_ok={det_ok}, det_intent={det_intent!r})")

    llm_result = llm_resolve_query(query, client)
    llm_ticker_ok = validate_ticker(llm_result["ticker"])

    if llm_ticker_ok:
        final_ticker = llm_result["ticker"]
        source = "llm fallback"
    elif det_ok:
        final_ticker = det_ticker
        source = det_source
    else:
        final_ticker = last_symbol
        source = "last_symbol (llm+deterministic both failed)"

    final_intent = llm_result["intent"] if llm_result["intent"] != "chat" else det_intent

    result = {
        "ticker": final_ticker,
        "intent": final_intent,
        "source": source,
        "used_llm": True,
    }
    print(f"[PARSE] final (after LLM): {result}")
    return result
