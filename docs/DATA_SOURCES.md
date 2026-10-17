# Data sources register (PRD COMP-05)

This file lists every outside source ARIA reads from: what each one gives us, its terms, how we use it, and how we credit it. `tests/test_data_sources_register.py` fails when code adds a circuit-breaker source (`http.get(source=…)`, `http.session(…)`, `guarded_call(…)`) or a `tools/data` fetcher that is missing here.

> **Status: pending owner and legal review.** "Our reading" is an engineering summary, not legal advice. Before any public or commercial launch, the owner must confirm each source's terms, or replace it with a licensed feed (PRD DATA-02 records that decision as open). In the meantime ARIA is a non-commercial, education-only research tool (docs/ADVICE_POLICY.md).

## Market and fund data

| Breaker source | Code | Data | Terms | Our reading of permitted use | Attribution | Status |
|---|---|---|---|---|---|---|
| `yfinance` | `tools/data/fetcher.py`, `tools/data/providers/yahoo.py`, `utils/helpers.py` (symbol search) | Prices (OHLCV), quotes, financial statements, company info | [Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html); the yfinance library is unofficial | Personal and research use. Yahoo data may not be redistributed or used commercially without a licence | "Market data: Yahoo Finance (via yfinance)" | Pending: needs a licensed feed for any commercial use |
| `nse` | `tools/data/nse_scraper.py`, `tools/data/providers/nse.py`, `tools/data/pdf_scraper.py`, `scripts/refresh_instruments.py` | Bhavcopy (end-of-day prices), large deals, corporate actions, shareholding, top movers, instrument master, XBRL filings | [NSE terms of use](https://www.nseindia.com/terms-of-use) | The website forbids automated access and commercial reuse without NSE's permission. Real-time and commercial use need an NSE data licence (NSE Data & Analytics) | "Source: NSE India" | Pending: the largest risk; licence or drop it before launch |
| `bse` | `tools/data/pdf_scraper.py` (annual reports), `scripts/refresh_instruments.py` (BSE bhavcopy) | Annual-report links, BSE instrument list | [BSE terms](https://www.bseindia.com/static/about/disclaimer.aspx) | Personal use. Redistribution needs BSE's permission | "Source: BSE India" | Pending |
| `amfi` | `tools/data/amfi.py`, `scripts/verify_mf_returns.py` | Daily NAVs for all schemes | [AMFI disclaimer](https://www.amfiindia.com/disclaimer) | NAVs are published for public information. Credit AMFI and don't alter the values | "NAV data: AMFI" | Low risk; confirm |
| `mfapi` | `tools/data/mf_data.py` | NAV history per scheme (a community mirror of AMFI data) | [mfapi.in](https://www.mfapi.in/) (free, no SLA) | A free API with no stated restrictions. It derives from AMFI, so AMFI's terms apply | "NAV history: mfapi.in (AMFI data)" | Low risk; no SLA |

## News

| Breaker source | Code | Data | Terms | Our reading of permitted use | Attribution | Status |
|---|---|---|---|---|---|---|
| `google_news` | `tools/data/news_fetcher.py` | Headline, link and snippet from the Google News RSS search | [Google terms](https://policies.google.com/terms) | Headlines with links back to the publisher. No full articles are stored or shown | Each item shows its publisher and links to it | Pending |
| `rss:moneycontrol`, `rss:economic_times`, `rss:livemint`, `rss:business_std` | `tools/data/news_fetcher.py` (feeds in `config.NEWS_FEEDS`) | Each publisher's market RSS feed | Each publisher's RSS terms (personal, non-commercial use) | Headline, short snippet and link. Never the full text | Publisher name and link on every item | Pending |

## Derived and vendored data (no live call)

| Code | Built from | Notes |
|---|---|---|
| `tools/data/instruments.py` | `data/instruments/` (NSE/BSE instrument masters, refreshed by `scripts/refresh_instruments.py`) | Symbol resolution only (VAL-02) |
| `tools/data/bhavcopy_history.py` | NSE bhavcopy rows ARIA has already fetched | Per-symbol fallback history, kept in Postgres (`bhavcopy_bars`, DATA-10) |
| `tools/data/adjustments.py` | NSE corporate actions (the `nse` row above) | Split/bonus factors applied to the stored bhavcopy history when it is read (DATA-07) |
| `tools/data/mf_data.py` | AMFI and mfapi (above) | Category and return metrics |
| `tools/data/providers/fake.py` | Recorded fixtures in `data/fake/` | Tests and offline demos only (`ARIA_FAKE_DATA`) |
| `fundamentals_snapshot` table | yfinance, through `scripts/refresh_fundamentals.py` | Screener universe (DATA-05) |
| `data/market/nse_holidays.csv` | NSE holiday circulars | Market clock (DATA-06) |

## Other external services (not market data)

| Breaker source | Code | Purpose | Data sent |
|---|---|---|---|
| `telegram` | `utils/telegram_link.py`, `utils/watchdog.py`, `scripts/check_alerts.py` | Alerts to linked users and ops alerts to the operator | The alert text, sent to the user's own chat |
| `turnstile` | `web/captcha.py` | Optional CAPTCHA check (SEC-05) | The widget token and the client IP |
| LLM providers (Groq, Gemini, BYO, local) | `agents/llm_client.py` | Writing answers | Prompts with redacted user context; see docs/PRIVACY.md (processors) |

## Attribution in the product

- The app footer and the README name the sources: "Data: Yahoo Finance (via yfinance), NSE, BSE, AMFI, mfapi.in, Google News and publisher RSS".
- Answers cite their evidence: every fundamentals figure carries a source footnote (LLM-02), and every news item links to its publisher.
