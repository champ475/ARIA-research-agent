"""
config.py — Central configuration for ARIA v2
Groq key rotation + Gemini fallback, all free.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Groq key rotation ─────────────────────────────────────────────────────────
# Add as many keys as you have (all free at console.groq.com)
_raw_groq_keys = [
    os.getenv("GROQ_API_KEY_1", ""),
    os.getenv("GROQ_API_KEY_2", ""),
    os.getenv("GROQ_API_KEY_3", ""),
    os.getenv("GROQ_API_KEY_4", ""),
    os.getenv("GROQ_API_KEY_5", ""),
]
GROQ_API_KEYS = [k for k in _raw_groq_keys if k.strip()]

GROQ_MODEL       = "llama-3.3-70b-versatile"   # 14,400 req/day per key
GROQ_MODEL_FAST  = "gemma2-9b-it"              # lighter, for fast calls

# ── Gemini fallback ───────────────────────────────────────────────────────────
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL     = "gemini-2.0-flash"

PREFERRED_PROVIDER = os.getenv("PREFERRED_PROVIDER", "groq").lower()

# ── Agentic research graph (see RFC_AGENTIC_RESEARCH_GRAPH.md) ───────────────
USE_LANGGRAPH_ORCHESTRATOR = os.getenv("USE_LANGGRAPH_ORCHESTRATOR", "false").lower() == "true"

# ── Token budgets (tune to stay within daily limits) ─────────────────────────
TOKENS_SYNTHESIS  = 2000   # full report synthesis
TOKENS_COMPLEX    = 3000   # multi-part questions / comparisons — need more room to cover every sub-ask
TOKENS_AGENT      = 800    # sub-agent calls
TOKENS_FAST       = 200    # suggestions, tiny calls
LLM_TEMPERATURE   = 0.3
MAX_RETRIES       = 2      # per provider before fallback

# ── Data ──────────────────────────────────────────────────────────────────────
NSE_SUFFIX  = ".NS"
BSE_SUFFIX  = ".BO"
DATA_DIR    = "data"
REPORTS_DIR = "reports"

# ── Database (Neon Postgres — shared by the app and the GH Actions alert checker) ─
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Telegram (alert notifications) ────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

MACRO_TICKERS = {
    "Nifty 50"  : "^NSEI",
    "Sensex"    : "^BSESN",
    "Nifty Bank": "^NSEBANK",
    "USD/INR"   : "USDINR=X",
    "Crude Oil" : "CL=F",
    "Gold"      : "GC=F",
    "10Y Bond"  : "^TNX",
}

NEWS_FEEDS = {
    "moneycontrol"   : "https://www.moneycontrol.com/rss/latestnews.xml",
    "economic_times" : "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "livemint"       : "https://www.livemint.com/rss/markets",
    "business_std"   : "https://www.business-standard.com/rss/markets-106.rss",
}

# ── NSE/BSE scraping ──────────────────────────────────────────────────────────
NSE_BASE_URL        = "https://www.nseindia.com"
NSE_BHAVCOPY_URL    = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv"
NSE_BULK_DEALS_URL  = "https://www.nseindia.com/api/bulk-deal-data"
NSE_CORPORATE_URL   = "https://www.nseindia.com/api/corporates-corporateActions"

# ── TA defaults ───────────────────────────────────────────────────────────────
TA_RSI_PERIOD  = 14
TA_MACD_FAST   = 12
TA_MACD_SLOW   = 26
TA_MACD_SIGNAL = 9
TA_BB_PERIOD   = 20
TA_SMA_SHORT   = 50
TA_SMA_LONG    = 200

# ── Risk thresholds ───────────────────────────────────────────────────────────
RISK_HIGH_DE       = 2.0
RISK_LOW_CR        = 1.0
RISK_HIGH_PE       = 60.0
RISK_LOW_ROCE      = 10.0

# ── NSE sector universe (for screener & peer comparison) ─────────────────────
SECTOR_UNIVERSE = {
    "Technology": [
        "TCS","INFY","WIPRO","HCLTECH","TECHM","LTIM","PERSISTENT",
        "COFORGE","MPHASIS","OFSS","KPIT","TATAELXSI",
    ],
    "Financial Services": [
        "HDFCBANK","ICICIBANK","SBIN","KOTAKBANK","AXISBANK","INDUSINDBK",
        "BAJFINANCE","BAJAJFINSV","HDFC","ICICIGI","HDFCLIFE","SBILIFE",
    ],
    "Energy": [
        "RELIANCE","ONGC","BPCL","IOC","GAIL","COALINDIA","NTPC",
        "POWERGRID","ADANIGREEN","TATAPOWER","ADANIPOWER","CESC",
    ],
    "Consumer Cyclical": [
        "TITAN","MARUTI","TATAMOTORS","BAJAJ-AUTO","EICHERMOT",
        "HEROMOTOCO","TVS MOTOR","M&M","TRENT","DMART",
    ],
    "Healthcare": [
        "SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","APOLLOHOSP",
        "MANKIND","TORNTPHARM","LUPIN","ALKEM","IPCA",
    ],
    "Consumer Defensive": [
        "HINDUNILVR","ITC","NESTLEIND","DABUR","MARICO",
        "GODREJCP","BRITANNIA","TATACONSUM","VBL","RADICO",
    ],
    "Industrials": [
        "LT","SIEMENS","ABB","BHEL","HAL","BEL","CONCOR",
        "ADANIPORTS","IRFC","PFC","RECLTD",
    ],
    "Metals & Mining": [
        "TATASTEEL","HINDALCO","JSWSTEEL","SAIL","VEDL",
        "NMDC","HINDZINC","NATIONALUM","MOIL",
    ],
    "Real Estate": [
        "DLF","GODREJPROP","LODHA","OBEROIRLTY","PHOENIXLTD",
        "SOBHA","BRIGADE","PRESTIGE",
    ],
    "Telecom": [
        "BHARTIARTL","IDEA","INDUSTOWER","TATACOMM",
    ],
}

# Flattened stock universe across all sectors — historically named NIFTY200 but
# is actually ~100 curated NSE stocks, not the NIFTY 200 index constituents.
STOCK_UNIVERSE = [s for stocks in SECTOR_UNIVERSE.values() for s in stocks]
