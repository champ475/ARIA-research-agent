# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ARIA v2** (Autonomous Research & Investment Analyst) is a Python multi-agent investment research platform for Indian equity markets (NSE/BSE). It provides AI-powered stock analysis, institutional-grade report generation, mutual fund comparison, and stock screening via a Gradio web UI.

## Setup & Running

```bash
# Install dependencies
pip install -r requirements.txt

# Configure API keys (copy and edit)
cp .env.example .env
# Set GROQ_API_KEY_1, GEMINI_API_KEY (required); GROQ_API_KEY_2/3 optional for higher rate limits

# Run the app
python app.py
# Opens at http://localhost:7860
```

No build step required. No test suite exists — testing is done manually via the Gradio UI.

## Architecture

### Entry Point & UI
`app.py` builds a 4-tab Gradio interface (Equity Research, Report Generator, Mutual Funds, Screener). Agents are lazily initialized on first tab activation. Each tab has an independent agent instance with its own LLM conversation history.

### Agent Layer (`agents/`)
Each of the 4 agents (`equity_agent.py`, `report_agent.py`, `mf_agent.py`, `screener_agent.py`) extends `BaseAgent` in `base_agent.py`. The agent flow is:
1. Parse user intent from message (keyword/pattern matching)
2. Call tools to fetch and analyze data
3. Synthesize results via LLM
4. Return `(markdown_text, chart_html)` tuple

### LLM Provider (`agents/llm_client.py`)
`LLMClient` wraps Groq and Gemini providers with:
- Thread-safe round-robin key rotation across up to 5 Groq API keys (enables scaling free tier)
- Automatic fallback to Gemini 2.0 Flash on Groq 429 errors
- Per-instance conversation history (capped at 20 messages)

Primary model: `llama-3.3-70b-versatile`. Fast model: `gemma2-9b-it`. Fallback: `gemini-2.0-flash`.

### Tools Layer (`tools/`)
- `tools/data/` — Data fetchers: `fetcher.py` (yfinance wrapper), `nse_scraper.py` (bhavcopy, bulk/block deals), `news_fetcher.py` (RSS + Google News), `amfi.py` (mutual fund NAV), `cache.py` (Parquet caching to reduce API calls)
- `tools/analysis/` — Compute indicators: `technical.py` (RSI, MACD, Bollinger Bands), `fundamental.py` (PE, PB, ROE, ROCE), `risk.py` (volatility, beta), `backtester.py` (MA crossover, RSI strategies), `sentiment.py`
- `tools/charts/builder.py` — Plotly candlestick charts and heatmaps

### Configuration (`config.py`)
Central source of truth for: LLM token budgets (`TOKENS_SYNTHESIS=1500`, `TOKENS_AGENT=800`), sector universe (120+ NSE stocks across 9 sectors), technical analysis defaults (RSI 14, MACD 12/26/9, BB 20), risk thresholds, and data source URLs.

### Persistence (`utils/db.py`)
SQLite database with tables: `portfolio`, `watchlist`, `alerts`, `reports`, `screeners`. Initialized at startup via `init_db()`.

### Ticker Resolution (`utils/helpers.py`)
Maps common aliases to NSE symbols (e.g. `"SBI"` → `"SBIN"`, `"HUL"` → `"HINDUNILVR"`). All tickers must be suffixed with `.NS` for NSE or `.BO` for BSE when passed to yfinance.

## Changelog

Log at `C:\Users\arnav\Desktop\Arnav\PROJECTS\CHANGELOG_ARIA.md` (outside repo, survives branch switches).

**Update it for every major update, feature change, or fix.** New entry per change, newest on top, format:

```
## YYYY-MM-DD HH:MM IST — branch: `<branch-name>`
### <Title>
<Description — what changed, why, files touched>
```

Get branch via `git branch --show-current`, timestamp IST (`pytz` Asia/Kolkata). Skip trivial edits (typos, formatting, comments).

## Key Constraints

- **Token budgets**: Keep LLM calls within limits defined in `config.py`. Use `TOKENS_FAST` for suggestions, `TOKENS_AGENT` for sub-agent calls, `TOKENS_SYNTHESIS` for full analysis.
- **Rate limits**: Groq free tier is 14,400 req/day per key. The `_KeyRotator` in `llm_client.py` handles this — don't bypass it.
- **Data caching**: Use `tools/data/cache.py` for OHLCV data to avoid yfinance rate limits. Cache is Parquet format keyed by `{symbol}_{period}_{interval}`.
- **IST timezone**: All timestamps must use `pytz` Asia/Kolkata timezone (see `utils/helpers.py`).
