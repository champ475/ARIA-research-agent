# ARIA — Autonomous Research & Investment Analyst

Your AI research assistant for Indian stocks and mutual funds (NSE/BSE). Chat naturally — ARIA figures out what you need and handles it: stock research, IC-style reports, mutual fund advice, stock screening, and price/technical alerts sent straight to your phone.

Everything happens in one chat window. No tabs to hunt through, no menus to learn — just ask.

## Setup

**1. Install**

```bash
pip install -r requirements.txt
cp .env.example .env
```

**2. Get your free API keys and fill them into `.env`:**

| Key | Where to get it | Required? |
|---|---|---|
| `GROQ_API_KEY_1` | [console.groq.com](https://console.groq.com) | Yes |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Yes |
| `DATABASE_URL` | [neon.tech](https://neon.tech) — create a free project, copy the connection string | Yes |
| `GROQ_API_KEY_2`, `_3` | Same as above, extra keys | Optional — lets you chat more before hitting free-tier limits |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | See **Setting up Alerts** below | Only if you want alert notifications |

**3. Run it**

```bash
python app.py
```

Open **http://localhost:7860**, sign up with an email and password, and start chatting.

## Setting up Alerts (optional, one-time)

Alerts need a Telegram bot to notify you and a GitHub Actions cron job to check prices every 5 minutes even while your laptop is off.

1. **Create a Telegram bot** — message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, follow the prompts, copy the token it gives you into `TELEGRAM_BOT_TOKEN`.
2. **Get your chat ID** — send any message to your new bot, then message [@userinfobot](https://t.me/userinfobot) to get your numeric ID, put it in `TELEGRAM_CHAT_ID`.
3. **Push this repo to a public GitHub repo** (public keeps GitHub Actions free at this checking frequency).
4. **Add three repo secrets** — on GitHub, go to *Settings → Secrets and variables → Actions*, add `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` using the same values from your `.env`.

That's it — the checker runs automatically every 5 minutes during NSE market hours (9:15 AM–3:30 PM IST, weekdays) and pings your Telegram the moment an alert fires.

## How to use each feature

Just type what you want in the chat — ARIA routes it to the right specialist automatically.

### 📈 Equity Research
> "Analyse TCS"
> "Show me a chart for RELIANCE"
> "Compare HDFC Bank vs ICICI Bank"
> "What's the RSI and MACD on INFY?"
> "Backtest a moving average crossover on TATAMOTORS"

### 📄 Reports
> "Generate an IC report for RELIANCE"
> "Write a result update for TCS"
> "Sector thematic on IT stocks"
> "Show my saved reports"

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

Every alert fires **once** — you get a Telegram message, and the alert switches itself off so you don't get spammed. Set a new one anytime.

**Conditions you can set alerts on:** price above/below a level, % price move, RSI crossing a level, MACD bullish/bearish crossover, moving-average golden/death cross, Bollinger Band breakout, volume spike, new 52-week high/low, bulk deal activity, and portfolio P&L threshold.

### 📌 Watchlist
Use the Watchlist tab to add/remove stocks you're tracking — separate from alerts, just a quick-glance list.

## License

Private project — no license specified.
