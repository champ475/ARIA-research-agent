# Using ARIA

Type what you want in the **Chat** tab, in plain English. ARIA works out what kind of question it is and picks the right tools. You never choose a mode.

Every answer streams in live, links its numbers to sources, and shows a **data confidence** level (High, Medium or Low) when the underlying data was incomplete. A verdict built on thin data reads with less conviction than one built on a full data set.

## Equity research

```text
Analyse TCS
Show me a chart for RELIANCE
Compare HDFC Bank vs ICICI Bank
What's the RSI and MACD on INFY?
Backtest a moving average crossover on TATAMOTORS
```

You get fundamentals, technicals, recent news and sentiment, peer comparison and a risk profile, depending on what you asked. Banks and NBFCs are analysed on lender-appropriate measures, so ROCE and debt-to-equity are left out for them.

Follow-ups reuse the company you were discussing: after "Analyse TCS", ask "what about its RSI?".

## Reports

```text
Generate an IC report for RELIANCE
Write a result update for TCS
Sector thematic on IT stocks
Show my saved reports
Download report #12
```

Reports are stored in your account, not on the server's disk. Say "download report #12" any time to get the PDF or Markdown again.

## Mutual funds

```text
Compare large cap funds
Best ELSS funds for tax saving
Should I do SIP or lumpsum for a 10-year goal?
NAV of Parag Parikh Flexi Cap
```

## Screener

```text
Top gainers today
Stocks with PE below 20 and ROE above 15
Show bulk deals today
Save screener as my value screener
```

Filters understand "between", percentages and rupee amounts in crore. The answer lists the filters it applied and anything it did not understand. Results export as CSV. The screen runs over a snapshot of every NSE-listed stock, refreshed nightly.

## Alerts

```text
Alert me if TCS drops below 3500
Notify me when RELIANCE RSI crosses below 30
Alert me on a MACD bullish crossover for INFY
Notify me on a golden cross for HDFC Bank
Show my alerts
Cancel my TCS alert
Pause my RELIANCE alert
```

**Conditions you can use:** price above or below a level, percentage price move, RSI crossing a level, MACD bullish or bearish crossover, moving-average golden or death cross, Bollinger Band breakout, volume spike, new 52-week high or low, bulk deal activity, and portfolio P&L threshold.

An alert fires **once** by default: you get a Telegram message and it switches itself off, so you are not spammed. Ask for a **recurring** alert to keep getting notified each time the condition re-triggers (24-hour cooldown by default).

Alerts need a one-time Telegram link. Open **Settings → Telegram Alerts → Connect Telegram**. Operators: see [ALERTS.md](ALERTS.md) for the bot setup.

## Financial planning

```text
Help me plan for retirement in 20 years
I have ₹10L to invest for a house down payment in 5 years
Build me an investment strategy. I'm 28, moderate risk
```

ARIA asks a few questions (age, capital, horizon, goal, risk tolerance), remembers your answers across sessions, then builds a multi-asset allocation grounded in real fund and stock data. It shows model portfolios by risk band, not personal picks.

## Portfolio and watchlist

- **Portfolio tab.** Add holdings by hand or import a Zerodha or Groww holdings CSV. See live P&L and allocation.
- **Watchlist tab.** A quick-glance list of symbols you track. It is separate from alerts.

## Chat history

Chats are saved in the sidebar. You can rename them, search across all of them, page back through old ones, and export any chat as Markdown.

## Your account

Under **Settings** you can add your own LLM key, link Telegram, switch theme, download all your data as JSON, log out other devices and delete your account.

## What ARIA will not do

- Tell you to buy, sell or hold a specific security.
- Promise or predict returns.
- Cover markets outside Indian equities and mutual funds.

Ask for a pick and it reframes the question into an education-only answer. Details: [ADVICE_POLICY.md](ADVICE_POLICY.md).
