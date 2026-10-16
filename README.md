# ARIA — Autonomous Research & Investment Analyst

An autonomous financial research engine for Indian stocks and mutual funds (NSE/BSE). ARIA covers stock research, IC-style reports, mutual fund advice, stock screening, goal-based financial planning, and price/technical alerts sent straight to your phone.

You ask a question in plain language; a LangGraph research engine (see [docs/RFC_AGENTIC_RESEARCH_GRAPH.md](docs/RFC_AGENTIC_RESEARCH_GRAPH.md)) decomposes it into a plan, runs parallel evidence collection across equity/reports/mutual funds/screener/alerts/planning, verifies claims before synthesis, and answers with citations you can trace back to the underlying data.

## Features

- **📈 Equity research** — fundamentals (lender-aware: no ROCE or D/E for banks/NBFCs), technicals (RSI/MACD/Bollinger/moving averages), news and headline sentiment, charts, same-industry peer comparison, a 0–100 risk profile, and MA-crossover/RSI backtests with trading costs — streamed live token-by-token.
- **📄 IC-style reports** — initiating coverage, result updates, sector thematics, quick notes — downloadable as PDF/Markdown.
- **💰 Mutual funds** — NAV lookup, category comparison (large/mid/small cap, ELSS, debt, hybrid, index), SIP-vs-lumpsum guidance.
- **🔍 Screener** — filter the market by PE/ROE/ROCE/D-E/sector, top gainers/losers, bulk/block deals, save screens for reuse.
- **🔔 Price & technical alerts** — 14 condition types, delivered to your own Telegram chat the moment they fire (checked every 5 min, even while your machine is off).
- **🎯 Goal-based planning** — multi-turn intake (age, capital, horizon, goal, risk tolerance), remembered across sessions, then a real allocation grounded in actual fund/stock data.
- **📌 Watchlist** — a quick-glance tracked-symbol list, separate from alerts.
- **⚙️ Bring your own LLM key** — 5 free messages/day on the app's keys, then plug in any OpenAI-compatible provider (OpenAI, Groq, OpenRouter, Together, Fireworks, Ollama, ...) for unlimited use, encrypted at rest.
- **Follows the conversation** — like ChatGPT, a follow-up ("what about its RSI?") resolves against what you were just discussing, no need to repeat the ticker.
- **Cites its numbers** — every ratio/claim links back to the exact evidence it came from, with an explicit confidence level when the underlying data was thin.

## Setup

**1. Install**

```bash
pip install -r requirements.txt
cp .env.example .env
```

**2. Get your free API keys and fill them into `.env`:**

| Key | Where to get it | Required? |
|---|---|---|
| `SECRET_KEY` | Any random string you make up | Yes — encrypts users' own LLM keys before storing them |
| `DATABASE_URL` | [neon.tech](https://neon.tech) — create a free project, copy the connection string | Yes (skip if using Docker Compose — see below) |
| `GROQ_API_KEY_1`, `GEMINI_API_KEY` | [console.groq.com](https://console.groq.com), [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Optional — powers the built-in free trial (see below); skip both if you'd rather everyone bring their own key from day one |
| `GROQ_API_KEY_2`, `_3` | Same as above, extra keys | Optional — lets more trial users chat before hitting free-tier limits |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME` | See **Setting up Alerts** below | Only if you want alert notifications |

**Free trial vs. bring your own key:** every signup gets 5 free messages/day on your Groq/Gemini keys above. Beyond that (or from message one, if you skip those keys), each user pastes their own key under the **⚙️ Settings** tab in-app — any OpenAI-compatible provider works (OpenAI, Groq, OpenRouter, Together, Fireworks, Ollama, ...), so you're never on the hook for a stranger's usage.

**3. Create the database schema, then run it**

```bash
python scripts/migrate.py   # applies migrations; safe to re-run, needed after every update
python app.py
```

The app checks the schema version at startup and refuses to start (with this instruction) if migrations haven't been applied.

**Background jobs** (alert checks in market hours, housekeeping, the Telegram link listener, the nightly fundamentals snapshot and fund metrics) live in `worker.py`. In dev (`APP_ENV=dev`, the default) the app runs them in-process. Anywhere else, run `python worker.py` next to the app. Each job holds a Postgres advisory lock, so extra workers or app replicas never double-run anything. If `DATABASE_URL` points at a connection pooler (Neon's `-pooler` host), set `JOBS_DATABASE_URL` to the direct host: advisory locks need a real session.

Open **http://localhost:7860**, sign up with an email and password, and start chatting.

### Or run it with Docker

No local Python, Postgres, or Neon account needed — Compose bundles a Postgres container for you. Still need `SECRET_KEY` in `.env` (and `GROQ_API_KEY_1`/`GEMINI_API_KEY` if you want to offer the free trial — see above).

```bash
cp .env.example .env   # fill in SECRET_KEY at minimum
docker compose up --build
```

Open **http://localhost:7860**. Data persists in a named Docker volume (`aria_pg_data`) across restarts. Compose also starts a `worker` service for the background jobs.

### Or deploy it for free on Hugging Face Spaces

No server to manage, no card required. This works cleanly with the free tier specifically because nothing in this app depends on the host's disk surviving a restart — reports render on demand from Postgres (not a local file), and alerts run entirely on GitHub Actions, independent of whether the Space is asleep or awake.

1. Create a new Space at [huggingface.co/new-space](https://huggingface.co/new-space) — pick **Docker** as the SDK.
2. Push this repo's contents to the Space's git remote (shown on the Space's page after creation), or connect it to sync from your GitHub fork. The existing `Dockerfile` runs as-is — no changes needed.
3. Under the Space's **Settings → Variables and secrets**, add the same values from your `.env`: `SECRET_KEY` and `DATABASE_URL` at minimum, plus `GROQ_API_KEY_1`/`GEMINI_API_KEY` if you want to offer the free trial, and `TELEGRAM_BOT_TOKEN`/`TELEGRAM_BOT_USERNAME` if you want alerts.
4. The Space builds and starts automatically — open its URL, sign up, and start chatting.

Free-tier Spaces sleep after a period of inactivity and cold-start on the next visit (a few seconds) — this only affects the chat UI itself, never alert delivery (see **Setting up Alerts** below for why).

## Setting up Alerts (optional, one-time)

One Telegram bot serves every user — each person links their own chat in-app, there's no per-user bot setup. You (the operator) set this up once:

1. **Create a Telegram bot** — message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, follow the prompts, copy the token it gives you into `TELEGRAM_BOT_TOKEN` and the username it gives you (without the `@`) into `TELEGRAM_BOT_USERNAME`.
2. **Push this repo to a public GitHub repo** (public keeps GitHub Actions free at this checking frequency).
3. **Add two repo secrets** — on GitHub, go to *Settings → Secrets and variables → Actions*, add `DATABASE_URL` and `TELEGRAM_BOT_TOKEN` using the same values from your `.env`.

That's it on your end — the checker runs automatically every 5 minutes during NSE market hours (9:15 AM–3:30 PM IST, weekdays).

**Each user connects their own chat** (no setup needed from you beyond the above): in-app, under **⚙️ Settings → 🔔 Telegram Alerts**, click "Connect Telegram" — it opens a link that starts a chat with your bot and links it to their account automatically. Alerts they create fire to that chat, and only that chat.

## What to expect

Type in plain English, no command syntax. Example:

> **You:** what is the PE ratio of TCS
>
> **ARIA:** The P/E ratio of TCS is 24.3 [^pe].
>
> ---
> ### 🧮 Cited calculations
> **[^pe] P/E Ratio**
> 24.3 = current price (₹4,127.50) / trailing EPS (₹169.85)

> **You:** what about its RSI
>
> **ARIA:** *(follow-up resolves "its" back to TCS automatically)* TCS's 14-day RSI is 58.2 — neither overbought nor oversold.

Ask for a chart, a comparison, a report, an alert, or a fund lookup and ARIA routes it to the right specialist itself — you never pick a mode or tab.

## How to use each feature

Just type what you want in the chat — ARIA routes it to the right specialist automatically.

### 📈 Equity Research
> "Analyse TCS"
> "Show me a chart for RELIANCE"
> "Compare HDFC Bank vs ICICI Bank"
> "What's the RSI and MACD on INFY?"
> "Backtest a moving average crossover on TATAMOTORS"

Responses stream in live, flag a **DATA CONFIDENCE** level (High/Medium/Low) whenever underlying data is incomplete, and carry footnote citations (`[^pe]`, `[^roce]`, etc.) back to the exact number they're based on — so a verdict answered off thin data reads with less conviction than one backed by a full data set.

### 📄 Reports
> "Generate an IC report for RELIANCE"
> "Write a result update for TCS"
> "Sector thematic on IT stocks"
> "Show my saved reports"
> "Download report #12"

Every report's full text lives in your account, not on the server's disk — say "download report #&lt;id&gt;" anytime to get the PDF/Markdown again, regenerated on the spot.

### 💰 Mutual Funds
> "Compare large cap funds"
> "Best ELSS funds for tax saving"
> "Should I do SIP or lumpsum for a 10-year goal?"
> "NAV of Parag Parikh Flexi Cap"

### 🔍 Screener
> "Top gainers today"
> "Stocks with PE below 20 and ROE above 15"
> "Show bulk deals today"
> "Save screener as my value screener"

### 🔔 Alerts
> "Alert me if TCS drops below 3500"
> "Notify me when RELIANCE RSI crosses below 30"
> "Alert me on a MACD bullish crossover for INFY"
> "Notify me on a golden cross for HDFC Bank"
> "Show my alerts"
> "Cancel my TCS alert"
> "Pause my RELIANCE alert"

By default an alert fires **once** — you get a Telegram message, and it switches itself off so you don't get spammed. Ask for a **recurring** alert instead (with a cooldown, default 24h) if you want it to keep firing each time the condition re-triggers.

**Conditions you can set alerts on:** price above/below a level, % price move, RSI crossing a level, MACD bullish/bearish crossover, moving-average golden/death cross, Bollinger Band breakout, volume spike, new 52-week high/low, bulk deal activity, and portfolio P&L threshold.

### 🎯 Financial Planning
> "Help me plan for retirement in 20 years"
> "I have ₹10L to invest for a house down payment in 5 years"
> "Build me an investment strategy — I'm 28, moderate risk"

ARIA asks a few questions to fill in your profile (age, capital, horizon, goal, risk tolerance — remembered across sessions), then designs a multi-asset allocation grounded in real fund/stock data rather than a generic template.

### 📌 Watchlist
Use the Watchlist tab to add/remove stocks you're tracking — separate from alerts, just a quick-glance list.

## Contributing

Bug reports, feature requests, and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, test running, and how to add a new agent or tool.

## Data sources

Data: Yahoo Finance (via yfinance), NSE, BSE, AMFI, mfapi.in, Google News and publisher RSS. These are free public sources, used for non-commercial, education-only research. Each one's terms, permitted use and attribution are in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md). Before any commercial use, read that register's status column: NSE and Yahoo data need a licence.

## Legal

ARIA is a research tool, not financial advice — see [LEGAL.md](LEGAL.md) for the full terms of use and privacy notice every user agrees to at signup (not investment advice, no SEBI registration, AI output may be wrong, third-party data has no accuracy guarantee, where your data goes if you bring your own LLM key). If you're operating an instance for other people, read it and make sure it actually reflects your setup before shipping it as-is.

## License

[MIT](LICENSE)
