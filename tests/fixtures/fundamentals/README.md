# Golden fundamentals fixtures (DATA-03)

There are 15 companies, frozen from Yahoo Finance by `scripts/freeze_fundamentals.py` on 16 Oct 2026:

| Group | Companies |
|---|---|
| IT | TCS, INFY, WIPRO, HCLTECH, TECHM |
| Lenders | HDFCBANK, ICICIBANK, SBIN, KOTAKBANK, BAJFINANCE |
| Others | RELIANCE, ITC, LT, MARUTI, SUNPHARMA |

Each `<SYMBOL>.json` holds the quote fields we use, the last two fiscal years of the annual statements, and a `reference` block. On 18 Oct 2026, `--merge-info` added the dividend and analyst fields (`dividendYield`, `dividendRate`, `trailingAnnualDividendYield`, `payoutRatio`, `targetMeanPrice`, `numberOfAnalystOpinions`) without touching the statements. The time of that merge is `info_merged_at`. The answer eval (`evals/answers/`) serves these files as its market data, and its reference facts are read from them. `tests/tools/test_golden_fundamentals.py` checks `tools/analysis/fundamental.py` against that block to within ±2%. `../ohlcv/{TCS,HDFCBANK,RELIANCE}.csv` hold one year of daily bars for the indicator checks.

## How the reference values are computed

`reference()` in the freeze script is written separately from the app code on purpose. It uses plain formulas on rows matched by their exact names:

| Ratio | Formula | Statement period |
|---|---|---|
| P/E | price ÷ trailing EPS | Yahoo's trailing 12 months (TTM), consolidated |
| P/B | price ÷ book value per share | latest balance sheet |
| ROE | Net Income ÷ Stockholders Equity | latest fiscal year (FY), year-end equity |
| Net margin | Net Income ÷ Total Revenue | latest FY |
| ROCE | EBIT ÷ (Total Assets − Current Liabilities) | latest FY, not for lenders |
| D/E | Total Debt ÷ Stockholders Equity | latest FY, not for lenders |
| Current ratio | Current Assets ÷ Current Liabilities | latest FY, not for lenders |

P/E and P/B are also compared with Yahoo's own `trailingPE` and `priceToBook`.

## Methodology differences from published figures

The cited-calculation footnotes mention these differences:

- **ROE.** We divide by year-end equity. Annual reports and Yahoo's `returnOnEquity` use average equity and TTM profit, so the figures differ by 1–3 points. For example, TCS is 45.9% for us and 45.6% on Yahoo. WIPRO is 14.9% for us and 16.1% on Yahoo.
- **ROCE.** We use EBIT ÷ (total assets − current liabilities) for the latest year. Annual reports often use average capital employed.
- **P/E.** We use consolidated TTM EPS. NSE's website quotes standalone P/E.
- **Current ratio and D/E.** We use the fiscal-year balance sheet. Yahoo's `currentRatio` and `debtToEquity` are from the most recent quarter. For example, TCS's current ratio is 2.23 at fiscal year-end and 2.48 for the most recent quarter.

## Data problems these fixtures exposed

- **Wrong rows were matched.** Plain substring matching read EBIT from "Normalized EBITDA" and revenue from "Reconciled Cost Of Revenue", because yfinance lists those rows first. Row matching is now exact-name-first.
- **`debtToEquity` is always a percentage.** Yahoo reports TCS 9.83 (= 0.098x) and MARUTI 0.096 (= 0.001x). The old fallback divided by 100 only when the value was above 20, so TCS would have shown a D/E of 9.83x.
- **HDFCBANK equity looks inflated.** On Yahoo's balance sheet, HDFCBANK's Stockholders Equity is ₹8.17 lakh crore. Yahoo's own book value per share implies about ₹6.0 lakh crore. As a result, statement-based ROE is 8.6%, against 13.8% from Yahoo's `returnOnEquity`. This was left unchanged in the fixture as a known source-data gap. It is a reason to prefer a licensed source (DATA-02 ADR).
- **Lenders need different metrics.** Banks and NBFCs have no EBIT row and no current/non-current split. ROCE, D/E and the current ratio are suppressed for them (`tools/analysis/sector_profile.py`). Answers show NII ÷ total assets as a labelled NIM proxy. Gross NPA and CASA are not in this data source.

## Not done (deviation)

- The reference values are independent recomputations plus Yahoo's reported figures. They are not transcribed from annual-report PDFs.
- No TradingView export was available. The indicators are checked against the published definitions (Wilder RSI, EMA MACD, population-σ Bollinger) through both the `ta` path and the fallback path.
