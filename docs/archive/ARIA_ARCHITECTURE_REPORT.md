# ARIA v2 — System Architecture Report

*Autonomous Research & Investment Analyst — Indian equity/MF research platform*
*Report date: 2026-07-23 · Branch: `feature/analyst-trust-reliability`*

> **Note on source docs**: `CLAUDE.md` in the repo is stale in three ways vs. actual code: it describes a 4-tab UI (actual: 2 tabs, chat-routed), SQLite (actual: Postgres/Neon), and 4 agents (actual: 6). `ARCHITECTURE.md`'s decision log is current and used as ground truth here, cross-checked directly against source.

---

## 1. System Architecture

### Overall diagram

```mermaid
flowchart TD
    U[User] -->|email/password| AUTH[Auth Gate\nutils/auth.py]
    AUTH --> UI[Gradio Blocks UI\napp.py]
    UI -->|Chat tab| CHAT[msg_box submit]
    UI -->|Watchlist tab| WL[Watchlist HTML table]

    CHAT --> ORCH[Orchestrator\nagents/orchestrator.py]
    ORCH -->|keyword-score routing| ROUTE{route()}

    ROUTE -->|default| EQ[EquityAgent]
    ROUTE -->|report kw| RPT[ReportAgent]
    ROUTE -->|mf kw| MF[MFAgent]
    ROUTE -->|screener kw| SCR[ScreenerAgent]
    ROUTE -->|alert kw / CRUD regex| ALT[AlertAgent]
    ROUTE -->|planning kw / sticky| PLN[PlanningAgent]

    EQ --> BUNDLE[_fetch_bundle\nThreadPoolExecutor x6]
    BUNDLE --> FETCH[tools/data/fetcher.py]
    BUNDLE --> TECH[tools/analysis/technical.py]
    BUNDLE --> FUND[tools/analysis/fundamental.py]
    BUNDLE --> RISK[tools/analysis/risk.py]
    BUNDLE --> SENT[tools/analysis/sentiment.py]
    BUNDLE --> PDF[tools/data/pdf_scraper.py]
    BUNDLE --> NEWS[tools/data/news_fetcher.py]

    FETCH --> CACHE[(Parquet cache\ntools/data/cache.py)]
    FETCH -->|degraded fallback| NSE[nse_scraper.py\nbhavcopy]
    FETCH --> YF[(yfinance)]

    EQ --> LLM[LLMClient\nagents/llm_client.py]
    RPT --> LLM
    MF --> LLM
    SCR --> LLM
    ALT --> LLM
    PLN --> LLM

    LLM -->|primary| GROQ[Groq\nllama-3.3-70b-versatile]
    LLM -->|429 fallback| GEMINI[Gemini 2.0 Flash]

    LLM --> ORCH
    ORCH -->|stream text, chart, pdf, md, csv, tab| UI
    UI --> DB[(Postgres / Neon\nutils/db.py)]

    CRON[GitHub Actions\nevery 5 min] --> CHECK[scripts/check_alerts.py]
    CHECK --> DB
    CHECK -->|match| TG[Telegram notification]
```

### Request flow, frontend → LLM → tools → back

1. User authenticates (`utils/auth.py`, PBKDF2 via stdlib `hashlib`/`secrets` — no external auth library) → `app.py` reveals the main `gr.Blocks` group.
2. User types a message in the Chat tab → `_respond()` (`app.py:610`) fires on submit/click.
3. `get_orchestrator(user_id)` (`app.py:29`) returns a per-user cached `Orchestrator` instance (module dict `_orchestrators`).
4. `Orchestrator.route(message, last_tab)` (`orchestrator.py:73`) — deterministic keyword-set scoring across 6 categories, equity is the score-0 default. A `last_tab == "planning"` stickiness rule keeps multi-turn intake threads from falling out to equity mid-flow.
5. Dispatch to the winning specialist agent. Only **EquityAgent** streams token-by-token (`process_stream()`, checked via `hasattr`); the rest yield once.
6. The agent fetches whatever data it needs from `tools/` (parallelized in EquityAgent via a 6-worker `ThreadPoolExecutor`), builds a prompt bundling that data + user question, and calls `LLMClient`.
7. `LLMClient` round-robins Groq API keys, falls back to Gemini on 429s (see §6), returns/streams text.
8. `Orchestrator.process_stream()` normalizes every agent's differing return shape into `(text, chart, pdf_path, md_path, csv_path, tab_used)` and re-renders the chat bubble on each yield.
9. On completion: `generate_suggestions()` populates 4 follow-up chips; `_persist_turn()` writes both turns to Postgres (`chat_sessions`/`chat_history`).

---

## 2. Repository Walkthrough

| Folder | Responsibility |
|---|---|
| `app.py` | Entry point. Gradio Blocks UI, auth gate, chat + watchlist tabs, orchestrator wiring, DB init at import time, `demo.launch()`. |
| `agents/` | 6 specialist agents + orchestrator + LLM client wrapper. |
| `tools/data/` | External data fetchers: yfinance wrapper, NSE scraper, news RSS, AMFI mutual fund NAV, PDF scraper, Parquet cache. |
| `tools/analysis/` | Pure computation: technical indicators, fundamental ratios (recomputed from raw statements), risk scoring, sentiment, backtesting. |
| `tools/charts/builder.py` | Plotly chart construction (candlestick, heatmap, portfolio, backtest curve). |
| `tools/portfolio.py`, `tools/screener.py` | Thin wrappers — live P&L calc, filter-based stock screening. |
| `config.py` | Central constants: token budgets, model names, sector universe, TA defaults, risk thresholds, data source URLs. |
| `utils/db.py` | Postgres (Neon) connection pool + all schema/queries. |
| `utils/helpers.py` | Ticker resolution, IST timestamps, intent keyword map. |
| `utils/auth.py` | Password hashing, session tokens. |
| `scripts/check_alerts.py` | Out-of-process alert evaluator, run via cron. |
| `.github/workflows/alert-check.yml` | GitHub Actions cron (`*/5 * * * *`) driving the alert checker. |
| `reports/` | Generated `.md`/`.pdf` report artifacts on disk. |
| `data/parquet/` | Parquet OHLCV cache files. |
| `ARCHITECTURE.md` | Decision log (Problem/Alternatives/Decision/Trade-offs/Future) — authoritative for *why*. |
| `CLAUDE.md` | Onboarding doc — **partially stale**, see note above. |

**Entry point**: `app.py`. `init_db()` runs at import time (line 14); `if __name__ == "__main__"` calls `demo.launch(server_name="0.0.0.0", server_port=7860, share=False, ...)` with a JS hook forcing Gradio's dark theme (`document.body.classList.add('dark')`).

---

## 3. Agent Architecture

**6 agents**, each a standalone class constructing its own `LLMClient(system_prompt=SYSTEM)` — `BaseAgent` exists but is only lightly used (vestigial thin base with `ask()`/`chat()` capped at `TOKENS_AGENT`).

| Agent | Responsibility | Why separate |
|---|---|---|
| `EquityAgent` | Full stock research: fundamentals, technicals, risk, sentiment, quarterly filings, news, portfolio commands, backtesting, market overview. 615 lines — largest agent. | Distinct data bundle (6 parallel fetches), distinct grounding/citation logic, distinct streaming path — none of this transfers to other domains. |
| `ReportAgent` | 4 report types (Initiating Coverage, Result Update, Sector Thematic, Quick Note) as full Markdown/PDF documents saved to disk + DB. | Templates and disclosure boilerplate are report-specific; output is a *document*, not a chat reply — different rendering pipeline (`markdown` + `weasyprint`). |
| `MFAgent` | Mutual fund category routing, comparison, allocation advice against AMFI NAV data. | Entirely different data source (AMFI, not yfinance) and vocabulary (cap categories, ELSS, expense ratio). |
| `ScreenerAgent` | Factor screens, top movers, bulk/block deals, saved screener CRUD, CSV export. | Operates over the whole ~100-stock universe per query, not a single ticker — different performance profile and output shape (tables, not narrative). |
| `AlertAgent` | Natural-language CRUD for 14 alert types against Postgres. | Pure data-entry/extraction task; two-stage regex-then-LLM extraction is unlike any other agent's job. Evaluation logic deliberately lives *outside* this agent (see §9). |
| `PlanningAgent` | Multi-turn goal-based financial planning: profile intake, then LLM-designed multi-asset allocation strategy. | Long-lived, stateful, cross-session conversation (Postgres-persisted profile) — structurally a wizard, not a single-turn Q&A. |

**Why not one large agent**: each agent owns a distinct data source, output shape (chat text vs. PDF report vs. CSV vs. multi-turn intake), and prompt-engineering concern (grounding/citations for equity, template compliance for reports, extraction accuracy for alerts). Routing is a cheap deterministic keyword-scorer (`orchestrator.py`) rather than an LLM call — `ARCHITECTURE.md`'s 2026-07-20 entry records this as a deliberate choice: LLM-based routing was considered and rejected for cost/latency/non-determinism versus a keyword scorer that's "good enough" and free.

---

## 4. Prompt Design

### System prompts

- **EquityAgent** (`agents/equity_agent.py:27`, `SYSTEM`) — the richest prompt in the codebase. Covers: response style, citation-marker rules (`[^pe]` etc.), mandatory acknowledgment of the `DATA CONFIDENCE:` line, multi-part question coverage, trade-setup formatting (explicitly separating SHORT-TERM/1Y levels from LONG-TERM/2Y levels), and a not-investment-advice closer.
- **ReportAgent** — no single system prompt; each report type (`_initiating_coverage()` etc.) embeds its own full Markdown section template directly in the prompt string sent per-call.
- **AlertAgent** — `_EXTRACT_PROMPT` (`alert_agent.py:46`), an LLM JSON-extraction prompt for the 12 alert types too complex for regex.
- **PlanningAgent** — `_INTAKE_PROMPT` (`planning_agent.py:74`) for conversational profile extraction; a second unnamed strategy-synthesis prompt built in `_build_strategy()` that explicitly instructs against generic "60/40 for everyone" answers and requires every named fund/stock to be grounded in the supplied data bundle.

### Planning prompts

PlanningAgent is the only genuinely multi-phase planner:
1. **Intake** (`_intake_turn()`): one combined LLM call per turn both extracts confidently-inferrable fields (`age, capital, horizon_years, goal, risk_tolerance` — `_REQUIRED`) and decides the next conversational question. Not a rigid form — the LLM controls pacing.
2. **Strategy synthesis** (`_build_strategy()`): once required fields are known, fetches AMFI data across 6 core fund categories + a quality-factor equity screen (ROE>15, D/E<1.5, ROCE>12) + macro/news, then lets the LLM freely design allocation. No hardcoded formula — a soft "100 − age" sanity note is appended afterward as a reference only, never enforced.

### Tool-selection prompts

There isn't an LLM-driven tool-selection step anywhere in the system — this is a deliberate architectural choice, not a gap:
- **Routing** (which agent handles a message) is pure keyword-set scoring (`orchestrator.py::route()`), no LLM call.
- **Ticker/intent resolution** (`utils/helpers.py::resolve_query()`) runs a deterministic pass first (alias map → regex → `INTENT_MAP` keyword match); an LLM JSON-extraction call (`llm_resolve_query()`) is invoked *only* as a fallback when the ticker is missing/invalid or intent falls through to the generic `"chat"` bucket — and its output is always re-validated against the curated ticker universe before being trusted.
- Within `EquityAgent`, which data to fetch is not LLM-decided either — `_fetch_bundle()` always fetches everything in parallel; the LLM only decides how to *use* what's handed to it.

### Output formatting prompts

- Footnote/citation system: `tools/analysis/fundamental.py::to_text()` embeds `[^pe]/[^pb]/[^roce]/[^de]/[^cr]` markers; `cited_footnotes()` renders only markers the LLM actually used in its response, keeping the reference list minimal and traceable.
- `_grounding_note()` (`equity_agent.py:359`) is a post-hoc formatting/safety pass: regex-extracts numeric claims from the LLM's own output, cross-checks against the source data block, and appends a "could not be automatically verified" warning if ≥4 substantial numbers and >50% don't trace back (cited numbers excluded).
- Report templates (`ReportAgent`) are the most rigid formatting constraint — fixed section order (Thesis → Overview → Industry → Financials → Valuation/DCF → Peer Comp → Technical Outlook → ESG → Risk Factors → Financial Summary → Rating/PT → Disclosures) baked directly into the prompt text.

---

## 5. Tool Implementations

### `tools/data/`

| Tool | Inputs → Outputs | Error handling |
|---|---|---|
| `fetcher.py` | ticker → info/OHLCV/financials/macro/options/recommendations/peers (yfinance) | In-memory 5-min TTL caches on top of Parquet cache; on empty yfinance response for both `.NS`/`.BO`, falls back to NSE bhavcopy (`nse_scraper.py`) and tags result `_degraded_source` so callers can flag staleness in the UI. |
| `cache.py` | symbol/period/interval → Parquet file `data/parquet/{symbol}_{period}_{interval}.parquet` | Returns `None` if file missing/older than 4h; write silently no-ops on empty DataFrames or write errors. |
| `nse_scraper.py` | none/date → bhavcopy DataFrame, bulk/block deals, corporate actions, top movers | Every function wrapped in try/except returning empty `DataFrame()`; primes a `requests.Session()` against nseindia.com first to avoid bot detection; tries last 5 calendar days, skipping weekends. |
| `news_fetcher.py` | company/query → news items (Google News RSS + fixed RSS feeds from `config.NEWS_FEEDS`) | `feedparser` parse failures degrade to empty list, not exceptions. |
| `amfi.py` | fund name/category → NAV, comparison | Downloads/parses AMFI's semicolon-delimited NAVAll.txt; 30-min in-memory TTL cache. |
| `pdf_scraper.py` | symbol/doc_type/period → extracted text (max 8000/10000 chars) | Results cached in Postgres `pdf_cache` table keyed `(symbol, doc_type, period)`; max 40 pages parsed via `pdfplumber`. |

### `tools/analysis/`

| Tool | Inputs → Outputs | Notes |
|---|---|---|
| `technical.py` | OHLCV df → SMA/EMA/BB/RSI/MACD/ATR/Stochastic/ADX/VWAP/Volume-ratio/Supertrend | Uses `ta` library if available, else hand-rolled fallback. Requires ≥30 rows or returns unchanged. `swing_levels()` is a custom numpy swing-high/low detector added because the classic pivot formula is insensitive to whether 1Y or 2Y history was fetched. `compute_rr()` validates `stop < entry < target` before computing risk:reward. |
| `fundamental.py` | raw info + financial statements → PE/PB/ROE/ROA/ROCE/D-E/Current Ratio + DCF | **Recomputes ratios from raw statements** rather than trusting yfinance fields, because yfinance's D/E is inconsistently percentage-vs-ratio across tickers (heuristic: `>20` → assume percentage, divide by 100). Every ratio carries a `*_detail` derivation string for explainability. 3-scenario 5-year DCF (Bear 6%/13%/3%, Base 10%/12%/4%, Bull 15%/11%/5%: growth/WACC/terminal). |
| `risk.py` | fundamentals + technicals + sentiment + macro → 0-100 weighted risk score | Weights: Valuation 15%, Leverage 20%, Liquidity 15%, Profitability 15%, Volatility 15%, Sentiment 10%, Macro 10%. Thresholds from `config.py`. |
| `sentiment.py` | news items → score/label | VADER + finance-domain word boosts/drags if available; falls back to keyword bull/bear counting. |
| `backtester.py` | OHLCV df, params → CAGR/vol/Sharpe/max-drawdown | Simple long-only MA-crossover and RSI strategies, ₹100,000 starting capital, no external backtest library. |

### `tools/charts/builder.py`

Plotly, TradingView-dark theme. `candlestick()` returns `None` (never an error string) below 5 data rows, so callers can cleanly skip chart rendering.

**Cross-cutting error-handling pattern**: nearly every network call across `tools/` is wrapped in broad try/except, degrading to empty DataFrame/dict/None/fallback string rather than raising — consistent defensive style, plus the newer `_degraded_source` tagging convention (vs. silent equivalence) for cases where data-quality matters downstream (confidence tiering).

---

## 6. Data Layer

### Database — Postgres (Neon)

`utils/db.py` uses `psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=10)`, `RealDictCursor`. `release_conn()` rolls back before returning to pool to avoid leaking aborted transactions. **This supersedes CLAUDE.md's SQLite description** — the migration is documented in `ARCHITECTURE.md`'s 2026-07-21 entry ("Shared Postgres (Neon) instead of per-process SQLite"), driven by the alert checker needing to see the same `alerts` table as the app process. A leftover `data/aria.db` file exists in-repo but is unread by current code.

**Schema** (all `CREATE TABLE IF NOT EXISTS` in `init_db()`):

| Table | Key columns | Notes |
|---|---|---|
| `users` | `id, email UNIQUE, password_hash, created_at` | |
| `sessions` | `id, user_id FK, token UNIQUE, expires_at, created_at` | Auth. |
| `portfolio` | `id, user_id FK, symbol, qty, avg_price, added_at` | |
| `watchlist` | `id, user_id FK, symbol, added_at`, `UNIQUE(user_id, symbol)` | |
| `alerts` | `id, user_id FK, symbol, alert_type CHECK IN (14 types), condition, threshold, active BOOL, created_at, triggered_at` | Partial index `WHERE active=TRUE`. Fires once then `active=FALSE`. |
| `cache_meta` | `key PK, fetched_at, ttl_seconds` | Declared but effectively unused — Parquet cache uses file mtime instead. |
| `chat_sessions` | `id, user_id FK, tab, title, created_at, updated_at` | |
| `chat_history` | `id, user_id FK, session_id FK, tab, role, content, ts` | |
| `saved_screeners` | `id, user_id FK, name, filters_json, created_at` | |
| `reports` | `id, user_id FK, symbol, report_type, title, content, created_at` | Full report markdown stored in DB *and* as files on disk. |
| `pdf_cache` | `id, symbol, doc_type, period, url, text_content, fetched_at`, `UNIQUE(symbol,doc_type,period)` | No `user_id` — intentionally shared fetch cache, not tenant data. |
| `financial_profiles` | `user_id PK/FK, profile JSONB, updated_at` | Schema-less JSON blob since PlanningAgent fields are LLM-extracted and open-ended. |

Tenant isolation is enforced at the query layer — mutating queries (`session_delete`, `chat_load`, `alert_remove`, etc.) all filter `WHERE ... AND user_id=%s`.

### User memory

Two layers, deliberately different lifetimes:
- **In-process conversation history**: `LLMClient` per-provider `self._history`, capped to the last 20 messages sent to the model (`_GroqProvider`/`_GeminiProvider`), reset per agent instance.
- **Persistent cross-session memory**: `chat_sessions`/`chat_history` in Postgres (full transcript, resumable — `load_history()` reseeds every specialist agent when a past session is reopened, since it's unknown up front which agent(s) touched that thread) and `financial_profiles` (durable structured profile for PlanningAgent, survives across sessions independent of chat transcript).

### Report storage

Dual-write: full Markdown content saved to Postgres `reports.content`, *and* rendered to `.md` + `.pdf` files on disk under `reports/` (`{symbol}_{report_type}_{timestamp}.md/.pdf`, via `markdown` → `weasyprint`).

### Alert storage

`alerts` table (schema above) — created via chat (`AlertAgent`), evaluated by a separate out-of-process script (`scripts/check_alerts.py`), fires once then deactivates. See §9 for the split rationale.

### Caching

- **OHLCV**: Parquet files, `data/parquet/{symbol}_{period}_{interval}.parquet`, 4-hour freshness window, file-mtime based (not `cache_meta`).
- **Quotes/financials**: in-memory 5-min TTL dicts in `fetcher.py`.
- **Bhavcopy**: cached once per calendar day (full-market CSV, expensive to refetch).
- **AMFI NAV**: in-memory, 30-min TTL.
- **PDF filings**: Postgres `pdf_cache`, keyed `(symbol, doc_type, period)`, effectively permanent (filings don't change once published).

---

## 7. LLM Stack

| Task | Model | Why |
|---|---|---|
| Primary synthesis (all agents) | `llama-3.3-70b-versatile` (Groq) | Free-tier-friendly, fast inference, strong enough for financial synthesis with grounding data supplied. |
| Fallback on Groq 429 | `gemini-2.0-flash` | Keeps the app usable when Groq's free-tier rate limit is hit, without a paid backup. |
| Reserved fast path (defined, not currently called from agent code) | `gemma2-9b-it` (Groq) | Presumably intended for cheap/low-latency sub-tasks (`TOKENS_FAST=200`) — not yet wired in. |

**Key rotation**: `_KeyRotator` round-robins up to 5 Groq API keys (`GROQ_API_KEY_1..5`), thread-safe, shared module-level instance across all agents/users in-process — multiplies effective free-tier throughput (5 × 14,400 req/day).

**Token budgets** (`config.py`): `TOKENS_SYNTHESIS=2000`, `TOKENS_COMPLEX=3000` (multi-part/comparison questions, detected via `_is_complex()`), `TOKENS_AGENT=800`, `TOKENS_FAST=200`, `LLM_TEMPERATURE=0.3`, `MAX_RETRIES=2` (→ 3 total attempts per call, exponential backoff `min(4**attempt, 16)`s on rate-limit errors).

**Cost/latency considerations**: everything is free-tier by design (no paid LLM spend assumed) — this shapes several decisions: keyword-based routing instead of an LLM router call (cheaper + deterministic + faster), deterministic-first ticker/intent resolution with LLM only as fallback, and aggressive multi-layer caching to avoid redundant fetches ahead of every LLM call. Streaming (EquityAgent only) improves *perceived* latency without changing total tokens.

---

## 8. Context Engineering

**Conversation history**: capped at last 20 messages sent to the model per provider (`_GroqProvider`/`_GeminiProvider`), per agent instance. Full untruncated history persists separately in Postgres `chat_history` for resumption — the 20-message cap only bounds what's sent to the LLM per call, not what's stored.

**Summarization**: none implemented — old messages are simply dropped past the 20-message window, not summarized. This is a real gap: long threads lose early context entirely rather than degrading gracefully.

**Retrieval**: no vector store / semantic retrieval anywhere in the stack. All "retrieval" is deterministic structured-data fetching (yfinance, AMFI, NSE, RSS) triggered per-turn by keyword/entity matching, not embedding search. PDF filing text is cached but searched via key match (`symbol, doc_type, period`), not similarity.

**Context compression**: the closest analog is EquityAgent's data-confidence tiering and citation-marker system — rather than compressing history, it compresses/structures the *current-turn data bundle* so the LLM gets a dense, labeled block (footnoted fundamentals, capped PDF excerpts at 8-10k chars) instead of raw dumps.

---

## 9. Evaluation

**No automated test suite, benchmark, or eval harness exists.** Confirmed absent from `requirements.txt` (no pytest/unittest), no `tests/` directory, no CI test job. `CLAUDE.md` states this explicitly: *"No test suite exists — testing is done manually via the Gradio UI."* `ARCHITECTURE.md`'s streaming-refactor entry self-flags the resulting risk: the non-streaming `process()` path is now only exercised incidentally, so a regression in `process_stream()` could silently break both paths.

**How correctness is currently verified** (informal, not automated):
- Manual UI testing per the CLAUDE.md workflow.
- Structural safeguards substitute for tests in a few places: `validate_ticker()` re-checks any LLM-returned ticker against the curated universe before trusting it; `compute_rr()` rejects malformed risk/reward inputs (`stop < entry < target`); DCF/ratio calculations are recomputed from raw statements rather than trusted from yfinance, specifically because yfinance was observed to be inconsistent (documented reasoning, not a test).

**Failure cases actively handled** (found in code, not a formal catalog):
- Missing/degraded market data → `_degraded_source` tagging → surfaced as lowered `confidence` in EquityAgent's output.
- Mid-stream LLM failure → cannot silently retry (would duplicate output already shown), so appends a visible "connection dropped" note instead.
- Unrecognized ticker in ReportAgent → shows help text rather than silently falling back to `_last_symbol` (deliberately chosen to avoid generating a report for the wrong company).
- Empty/insufficient OHLCV (<5 rows for charts, <30 for indicators) → functions return `None`/unchanged rather than erroring.

**Hallucination mitigation** — the most developed part of the "evaluation" story, though still heuristic rather than test-verified:
- **Grounding check** (`_grounding_note()`): post-hoc regex extraction of numeric claims from the LLM's response, cross-referenced against the actual data block; flags the response if too many numbers don't trace back.
- **Citation markers**: fundamentals carry footnote markers tied to the actual computed value, and only markers the LLM used are rendered — makes it possible to trace any cited number back to its source calculation.
- **Data confidence tiering**: explicit `DATA CONFIDENCE: High/Medium/Low` line the LLM must acknowledge, driven by a `gaps` list (degraded source, missing fundamentals/quarterly/risk/structural levels) — this is a signal *to* the model to hedge, not a guarantee it does.
- **Planning/report agents**: prompts explicitly instruct against generic boilerplate advice and require grounding every named fund/stock in the supplied data bundle — a prompt-level constraint, unverified at runtime.

None of these are tested against a labeled dataset or golden-output set — they're runtime heuristics, which is consistent with the project's "no test suite" state overall.

---

## 10. Future Roadmap

*(Synthesized from `ARCHITECTURE.md`'s "Future improvements" entries plus gaps visible in the current design — not a commitment list, worth validating with the project owner.)*

**Likely near-term candidates** (flagged directly in `ARCHITECTURE.md`):
- Formal handling for the streaming/non-streaming code-path divergence in agents, now that only EquityAgent streams — either extend streaming to other agents or collapse the dual-path complexity.
- Something to replace or supplement bhavcopy fallback with a more real-time degraded-data source, since bhavcopy is EOD-only (same-day intraday moves are invisible during a yfinance outage).
- Recurring (not fire-once) alerts, or explicit re-arm logic — current design intentionally fires once then deactivates, which is a real product gap for anything but one-shot notifications.

**Open/uncertain areas** (gaps observed, not confirmed roadmap items):
- No conversation summarization — long chat threads silently lose early context past 20 messages rather than degrading gracefully. Worth deciding if this is acceptable or needs a summarization pass.
- No automated tests — acceptable at current scale but risky as agent count/complexity grows (6 agents now, up from 4 the docs still describe).
- `GROQ_MODEL_FAST` (`gemma2-9b-it`) is defined in config but not wired into any agent — either use it for a genuine fast-path (e.g. intent classification, cheap extraction) or remove the dead config.
- `cache_meta` table exists but is unused by any query path — likely dead schema, candidate for removal or for actually wiring up cache invalidation through it.
- `NIFTY200` variable name is misleading (~100 stocks, not 200) — a documentation/naming cleanup, not a functional issue.
- Grounding/citation checks are heuristic and unverified against a labeled dataset — if hallucination rate matters for trust, a small golden-output eval set would close this gap meaningfully.

---

*Compiled from direct source inspection: `app.py`, `agents/*.py`, `tools/**/*.py`, `config.py`, `utils/db.py`, `utils/helpers.py`, `ARCHITECTURE.md`, `CLAUDE.md`, `requirements.txt`, `.github/workflows/alert-check.yml`.*
