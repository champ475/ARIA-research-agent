# ARIA v2 — Autonomous Research & Investment Analyst

Multi-agent investment research platform for Indian equity markets (NSE/BSE). AI-powered stock analysis, institutional-grade report generation, mutual fund comparison, stock screening — all via a Gradio web UI.

## Features

- **Equity Research** — analyse stocks, chart price action, backtest strategies, compare tickers, market overview
- **Report Generator** — IC (Investment Committee) reports, result updates, sector thematic notes
- **Mutual Funds** — fund comparison, SIP vs lumpsum advice, category screening (ELSS, large/mid cap)
- **Screener** — factor-based stock screening, PE/ROE filters, top gainers, bulk/block deal analysis
- **Free-tier LLM stack** — Groq (multi-key rotation) with automatic Gemini fallback on rate limits

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure API keys
cp .env.example .env
# Set GROQ_API_KEY_1, GEMINI_API_KEY (required)
# GROQ_API_KEY_2 / _3 optional — adds more free-tier request budget via rotation

# Run
python app.py
# → http://localhost:7860
```

No build step. No test suite — verify changes manually via the Gradio UI.

## Architecture

```
app.py                  Gradio UI, 4 tabs, lazy per-tab agent init
config.py                Central config: LLM budgets, sector universe, TA defaults, risk thresholds
agents/
  base_agent.py           Shared agent behavior
  equity_agent.py          Stock analysis, charting, backtesting, comparisons
  report_agent.py          IC reports, result updates, thematic notes
  mf_agent.py               Mutual fund comparison & advice
  screener_agent.py         Factor screening
  llm_client.py            Groq/Gemini wrapper: key rotation, fallback, conversation history
tools/
  data/                    fetcher.py (yfinance), nse_scraper.py (bhavcopy/deals),
                           news_fetcher.py (RSS/Google News), amfi.py (MF NAV), cache.py (Parquet)
  analysis/                technical.py, fundamental.py, risk.py, backtester.py, sentiment.py
  charts/builder.py        Plotly candlestick charts & heatmaps
  portfolio.py, screener.py
utils/
  db.py                    SQLite: portfolio, watchlist, alerts, reports, screeners
  helpers.py               Ticker alias resolution, IST timestamps
  pdf_export.py
data/                     Parquet cache + SQLite DB
reports/                  Generated report output
```

### Request flow

1. `app.py` builds a 4-tab Gradio interface. Each tab lazily instantiates its own agent on first use, with an independent LLM conversation history.
2. Agent parses user intent (keyword/pattern matching) → calls tools to fetch/compute data → synthesizes via LLM → returns `(markdown_text, chart_html)`.
3. `LLMClient` (`agents/llm_client.py`) round-robins across up to 5 Groq keys (thread-safe), falls back to Gemini 2.0 Flash on 429s. Conversation history capped at 20 messages per agent instance.

### Models

| Purpose      | Model                        |
|--------------|-------------------------------|
| Primary       | `llama-3.3-70b-versatile`     |
| Fast           | `gemma2-9b-it`                 |
| Fallback       | `gemini-2.0-flash`             |

## Configuration (`config.py`)

- Token budgets: `TOKENS_SYNTHESIS=1500`, `TOKENS_AGENT=800`, `TOKENS_FAST=200`
- Sector universe: 120+ NSE stocks across 9 sectors
- Technical analysis defaults: RSI 14, MACD 12/26/9, Bollinger Bands 20
- Risk thresholds, data source URLs (NSE bhavcopy, news feeds, AMFI)

## Key Constraints

- **Token budgets** — stay within `config.py` limits per call type
- **Rate limits** — Groq free tier: 14,400 req/day/key; `_KeyRotator` in `llm_client.py` handles rotation, don't bypass it
- **Data caching** — OHLCV data goes through `tools/data/cache.py` (Parquet, keyed `{symbol}_{period}_{interval}`) to avoid yfinance rate limits
- **Timezone** — all timestamps use `pytz` Asia/Kolkata (see `utils/helpers.py`)
- **Ticker format** — NSE tickers suffixed `.NS`, BSE `.BO`, before passing to yfinance; common aliases (`SBI`→`SBIN`, `HUL`→`HINDUNILVR`) resolved in `utils/helpers.py`

## Persistence

SQLite (`data/aria.db`) — tables: `portfolio`, `watchlist`, `alerts`, `reports`, `screeners`. Initialized on startup via `init_db()`.

## License

Private project — no license specified.
