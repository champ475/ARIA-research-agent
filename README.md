# ARIA v2 — Autonomous Research & Investment Analyst

Multi-agent investment research platform for Indian equity markets (NSE/BSE). AI-powered stock analysis, institutional-grade report generation, mutual fund comparison, stock screening, and price/technical alerts — all via a single chat-driven Gradio web UI.

## Features

- **Equity Research** — analyse stocks, chart price action, backtest strategies, compare tickers, market overview
- **Report Generator** — IC (Investment Committee) reports, result updates, sector thematic notes
- **Mutual Funds** — fund comparison, SIP vs lumpsum advice, category screening (ELSS, large/mid cap)
- **Screener** — factor-based stock screening, PE/ROE filters, top gainers, bulk/block deal analysis
- **Alerts** — chat-driven price and technical-indicator alerts (stop-loss, RSI/MACD crossovers, moving-average crosses, Bollinger breakouts, volume spikes, 52-week highs/lows, bulk deals, portfolio P&L), delivered via Telegram on a 5-min GitHub Actions cron during market hours
- **Free-tier LLM stack** — Groq (multi-key rotation) with automatic Gemini fallback on rate limits

A single orchestrator (`agents/orchestrator.py`) routes every chat message to the right specialist agent by keyword scoring — no manual tab-switching required.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure API keys and infra
cp .env.example .env
# Required: GROQ_API_KEY_1, GEMINI_API_KEY, DATABASE_URL (Neon Postgres — see below)
# Optional: GROQ_API_KEY_2 / _3 (more free-tier request budget via rotation),
#           TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (alert notifications)

# Run
python app.py
# → http://localhost:7860
```

No build step. No test suite — verify changes manually via the Gradio UI.

### Database

Persistence is a single shared Postgres DB (free tier on [Neon](https://neon.tech)) — used by both the local app and the GitHub Actions alert checker, since a local SQLite file isn't reachable from GH's cloud runners. Create a Neon project, put the pooled connection string in `DATABASE_URL`, and `init_db()` creates all tables on first run.

### Alerts (GitHub Actions)

`scripts/check_alerts.py` runs on a schedule (`.github/workflows/alert-check.yml`, every 5 min, market-hours-gated inside the script) and needs three repo secrets set under **Settings → Secrets and variables → Actions**: `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. The workflow file must live on the repo's default branch to be scheduled/dispatchable at all.

## Architecture

```
app.py                    Gradio UI: single unified chat tab + Watchlist tab, auth-gated
config.py                  Central config: LLM budgets, sector universe, TA defaults, risk thresholds, DB/Telegram env
agents/
  orchestrator.py           Routes each chat message to a specialist agent by keyword scoring
  base_agent.py             Shared agent behavior (optional convenience wrapper, not all agents inherit it)
  equity_agent.py            Stock analysis, charting, backtesting, comparisons
  report_agent.py            IC reports, result updates, thematic notes
  mf_agent.py                 Mutual fund comparison & advice
  screener_agent.py           Factor screening
  alert_agent.py               Chat-driven alert create/list/cancel/pause/resume (CRUD only — evaluation is scripts/check_alerts.py)
  llm_client.py              Groq/Gemini wrapper: key rotation, fallback, conversation history
tools/
  data/                      fetcher.py (yfinance), nse_scraper.py (bhavcopy/deals),
                             news_fetcher.py (RSS/Google News), amfi.py (MF NAV), cache.py (Parquet)
  analysis/                  technical.py, fundamental.py, risk.py, backtester.py, sentiment.py
  charts/builder.py          Plotly candlestick charts & heatmaps
  portfolio.py, screener.py
utils/
  db.py                      Postgres (Neon): users, sessions, portfolio, watchlist, alerts, chat, reports, screeners
  auth.py                    Signup/login/session (PBKDF2 password hashing, opaque session tokens)
  helpers.py                 Ticker alias resolution, IST timestamps, intent parsing
scripts/
  check_alerts.py            Standalone alert evaluator — run by GitHub Actions, not the app process
.github/workflows/
  alert-check.yml            Cron (every 5 min) + manual dispatch for the alert checker
data/                       Parquet OHLCV cache
reports/                    Generated report output
```

### Request flow

1. `app.py` builds a single chat tab (plus a Watchlist utility tab). Every message goes through `Orchestrator.process()`, which routes it to one specialist agent by keyword scoring, each with its own LLM conversation history.
2. Agent parses user intent (keyword/pattern matching) → calls tools to fetch/compute data (or, for alerts, does deterministic DB CRUD) → synthesizes via LLM where needed → returns `(markdown_text, chart_html)`.
3. `LLMClient` (`agents/llm_client.py`) round-robins across up to 5 Groq keys (thread-safe), falls back to Gemini 2.0 Flash on 429s. Conversation history capped at 20 messages per agent instance.
4. Alert *evaluation* happens outside the app entirely: `scripts/check_alerts.py`, triggered by GitHub Actions on a schedule, reads active alerts from the shared Postgres DB, evaluates conditions using the same `tools/data`/`tools/analysis` functions the chat agents use, and pushes Telegram notifications.

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
- **GitHub Actions minutes** — the alert-check cron runs every 5 min, 24/7 (market-hours gating happens inside the script, not the cron expression) — this only stays free on the GitHub Free plan if the repo is **public** (unlimited Actions minutes); a private repo would exceed the 2,000 min/month quota at this cadence

## Persistence

Postgres (Neon free tier) — single DB shared by the local app and the GitHub Actions alert checker. Tables: `users`, `sessions`, `portfolio`, `watchlist`, `alerts`, `chat_sessions`, `chat_history`, `saved_screeners`, `reports`, `pdf_cache`, `cache_meta`. Initialized on startup via `init_db()` (`utils/db.py`).

## License

Private project — no license specified.
