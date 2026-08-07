# ARIA v2 — Architecture Report (`main`, post-merge)

*Branch: `main` · after merging `feature/analyst-trust-reliability` (`0c1d084`)*
*This supersedes `ARIA_ARCHITECTURE_REPORT.md` for `main` specifically — that report was written from the `langgraph` branch, which additionally carries an experimental `graph/` LangGraph rewrite (see `ARIA_LANGGRAPH_WORKFLOW_REPORT.md`) that is **not** part of `main`. Everything below reflects `main` only.*

> `CLAUDE.md` remains stale on `main` in the same three ways flagged previously: it describes a 4-tab UI (actual: 2 tabs — Chat + Watchlist), SQLite (actual: Postgres/Neon), and 4 agents (actual: 6 — equity, report, mf, screener, alert, planning). `ARCHITECTURE.md`'s decision log is current and authoritative for *why* the system is shaped this way.

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

    EQ -->|stream()| LLM[LLMClient\nagents/llm_client.py]
    RPT --> LLM
    MF --> LLM
    SCR --> LLM
    ALT --> LLM
    PLN --> LLM

    LLM -->|primary| GROQ[Groq\nllama-3.3-70b-versatile]
    LLM -->|429 fallback, mid-call only\nnever after first chunk| GEMINI[Gemini 2.0 Flash]

    LLM --> ORCH
    ORCH -->|stream text, chart, pdf, md, csv, tab| UI
    UI --> DB[(Postgres / Neon\nutils/db.py)]

    CRON[GitHub Actions\nevery 5 min] --> CHECK[scripts/check_alerts.py]
    CHECK --> DB
    CHECK -->|match| TG[Telegram notification]
```

### Request flow, frontend → LLM → tools → back

1. User authenticates (`utils/auth.py`, PBKDF2 via stdlib `hashlib`/`secrets`) → `app.py` reveals the main `gr.Blocks` group (Chat + Watchlist tabs — confirmed at `app.py:837-841`).
2. Message submit fires `_respond()` (`app.py`) → `get_orchestrator(user_id)` returns a per-user cached `Orchestrator` (module dict `_orchestrators`).
3. `Orchestrator.route(message, last_tab)` — deterministic keyword-set scoring across 6 categories (`_REPORT_KW`/`_SCREENER_KW`/`_MF_KW`/`_ALERT_KW`/`_ALERT_CRUD_RE`/`_PLANNING_KW`), equity is the score-0 default. A `last_tab == "planning"` stickiness rule keeps multi-turn planning intake from falling out to equity mid-flow.
4. Dispatch to the winning specialist agent. **Only EquityAgent streams** token-by-token — the trust-reliability merge converted it to a `process_stream()` generator backed by `LLMClient.stream()` (new in this merge); the other 5 agents still yield once.
5. The agent fetches data from `tools/` (EquityAgent parallelizes 6 fetches via `ThreadPoolExecutor`), builds a prompt, calls `LLMClient`.
6. `LLMClient` round-robins up to 5 Groq keys, falls back Groq→Gemini on 429 — but **only before any chunk has been streamed**; a mid-stream failure now appends a visible "connection dropped" note instead of silently retrying (new fallback contract from this merge, since duplicating already-shown tokens would corrupt the transcript).
7. `Orchestrator.process_stream()` normalizes every agent's return shape into `(text, chart, pdf_path, md_path, csv_path, tab_used)`, re-rendering the chat bubble on each yield.
8. On completion: 4 follow-up suggestion chips generated; both turns persisted to Postgres (`chat_sessions`/`chat_history`).

---

## 2. Repository Walkthrough

| Folder | Responsibility |
|---|---|
| `app.py` | Entry point. Gradio Blocks UI (2 tabs: Chat, Watchlist), auth gate, orchestrator wiring, DB init at import time. |
| `agents/` | 6 specialist agents + orchestrator + LLM client wrapper. |
| `tools/data/` | External data fetchers: yfinance wrapper, NSE scraper, news RSS, AMFI mutual fund NAV, PDF scraper, Parquet cache. |
| `tools/analysis/` | Pure computation: technical indicators, fundamental ratios, risk scoring, sentiment, backtesting. |
| `tools/charts/builder.py` | Plotly chart construction. |
| `tools/portfolio.py`, `tools/screener.py` | Live P&L calc, filter-based stock screening. |
| `config.py` | Central constants: token budgets, model names, sector universe, TA defaults, risk thresholds, data source URLs. |
| `utils/db.py` | Postgres (Neon) connection pool + all schema/queries. |
| `utils/helpers.py` | Ticker resolution, IST timestamps, intent keyword map. |
| `utils/auth.py` | Password hashing, session tokens. |
| `scripts/check_alerts.py` | Out-of-process alert evaluator, run via cron. |
| `.github/workflows/alert-check.yml` | GitHub Actions cron (`*/5 * * * *`) driving the alert checker. |
| `reports/` | Generated `.md`/`.pdf` report artifacts on disk. |
| `ARCHITECTURE.md` | Decision log — authoritative for *why*, includes the two new 2026-07-23 entries from this merge. |
| `CLAUDE.md` | Onboarding doc — **stale**, see note above. |

**Entry point**: `app.py`. `init_db()` runs at import time; `demo.launch(server_name="0.0.0.0", server_port=7860, share=False)` with a JS hook forcing Gradio's dark theme.

---

## 3. Agent Architecture

**6 agents**, each constructing its own `LLMClient(system_prompt=SYSTEM)` directly rather than inheriting the mostly-vestigial `BaseAgent`.

| Agent | Responsibility | Why separate |
|---|---|---|
| `EquityAgent` | Full stock research: fundamentals, technicals, risk, sentiment, quarterly filings, news, portfolio commands, backtesting, market overview. Now streams. | Distinct data bundle, distinct grounding/citation logic (new this merge), distinct streaming path. |
| `ReportAgent` | 4 report types (IC, Result Update, Sector Thematic, Quick Note) as full Markdown/PDF documents. | Output is a *document* via a different rendering pipeline (`markdown`+`weasyprint`), not a chat reply. |
| `MFAgent` | Mutual fund category routing, comparison, allocation advice against AMFI NAV data. | Different data source and vocabulary entirely. |
| `ScreenerAgent` | Factor screens, top movers, bulk/block deals, saved screener CRUD, CSV export. | Operates over the whole ~100-stock universe, output is tabular not narrative. |
| `AlertAgent` | Natural-language CRUD for 14 alert types. | Extraction task, unlike any other agent's job; evaluation lives outside it (`scripts/check_alerts.py`). |
| `PlanningAgent` | Multi-turn goal-based financial planning: profile intake (persisted across sessions), then LLM-designed allocation strategy. | Long-lived, stateful wizard, not single-turn Q&A. |

Routing stays a cheap deterministic keyword-scorer, not an LLM call — `ARCHITECTURE.md`'s 2026-07-20 entry documents this as a deliberate cost/latency/determinism trade-off.

---

## 4. What This Merge Actually Changed

This is the headline delta vs. `main` before the merge — the two `ARCHITECTURE.md` entries dated 2026-07-23:

### 4.1 Data confidence tiering + inline citations
- `_fetch_bundle()` (EquityAgent) already knew when data was incomplete (missing quarterly results, empty 2Y structural levels, incomplete fundamentals) — that knowledge now shapes the verdict instead of scattering into "N/A" markers.
- `tools/analysis/fundamental.py::to_text()` tags the fundamentals text block with `[^key]` footnote markers (`pe`, `pb`, `roce`, `de`, `cr`); the model is instructed to keep the marker attached when it states that ratio; `cited_footnotes()` renders footnotes scoped only to markers actually used.
- A High/Medium/Low `DATA CONFIDENCE` line, computed from known gaps, is surfaced and the model must acknowledge it before giving a verdict.
- Cited numbers are exempted from the existing `_grounding_note()` hallucination heuristic — a `[^key]` marker is a stronger grounding signal than a raw digit match.
- **Trade-off** (from `ARCHITECTURE.md`): citation coverage is limited to the 5 ratios that already had a `*_detail` field; DCF and other derived figures aren't citable this way and are still shown unconditionally. The model can still choose not to cite a ratio it states — citation coverage is a floor, not a guarantee. Confidence tiering is a heuristic gap-count, not a calibrated probability.

### 4.2 Streaming LLM synthesis into the chat UI
- Full synthesis (2,000-3,000 output tokens) previously blocked the chat window 10-25s per turn. `EquityAgent` now exposes `process_stream()`; `LLMClient` gained `.stream()` on both providers.
- Fallback contract had to change to match: Groq→Gemini fallback only fires if the failure happens *before any chunk was yielded* — once tokens are on the wire, switching providers would duplicate output, so a mid-stream failure instead appends `"\n\n⚠️ *Connection to {provider} dropped mid-response...*"` and stops.
- Only `EquityAgent` streams; `Orchestrator.process_stream()` checks `hasattr(agent, "process_stream")` per agent and falls back to a single yield for the other 5.

---

## 5. Prompt Design

- **EquityAgent** `SYSTEM` prompt — richest in the codebase: response style, citation-marker rules, mandatory acknowledgment of `DATA CONFIDENCE:`, multi-part question coverage, trade-setup formatting (SHORT-TERM/1Y vs LONG-TERM/2Y levels split), not-investment-advice closer.
- **ReportAgent** — no single system prompt; each report type embeds its own full Markdown section template directly in the per-call prompt string.
- **AlertAgent** — `_EXTRACT_PROMPT`, LLM JSON-extraction for the 12 alert types too complex for the regex pre-filter.
- **PlanningAgent** — `_INTAKE_PROMPT` for conversational profile extraction; an unnamed strategy-synthesis prompt that explicitly forbids generic "60/40 for everyone" answers and requires every named fund/stock to be grounded in the supplied data bundle.
- **No LLM-driven tool selection anywhere** — routing is keyword scoring, ticker/intent resolution is deterministic-first with LLM as validated fallback, and EquityAgent's `_fetch_bundle()` always fetches everything rather than letting the LLM pick.
- **Output formatting**: `[^pe]/[^pb]/[^roce]/[^de]/[^cr]` citation markers (new); `_grounding_note()` post-hoc numeric cross-check, now citation-aware; rigid section-order Markdown templates for reports.

---

## 6. Tool Implementations

Unchanged from the pre-merge baseline except `tools/data/fetcher.py` (+117 lines) and `tools/analysis/fundamental.py` (+60 lines) for the confidence-tiering/citation work above.

| Tool | Inputs → Outputs | Error handling |
|---|---|---|
| `fetcher.py` | ticker → info/OHLCV/financials/macro/options/recommendations/peers | 5-min in-memory TTL on top of Parquet cache; NSE bhavcopy fallback tagged `_degraded_source` on yfinance miss, now feeding directly into confidence tiering. |
| `cache.py` | symbol/period/interval → Parquet | 4h freshness window, file-mtime based. |
| `nse_scraper.py` | → bhavcopy, bulk/block deals, corporate actions, top movers | try/except → empty `DataFrame()`; primed session against bot detection. |
| `news_fetcher.py` | company/query → news items | Google News RSS + `config.NEWS_FEEDS`. |
| `amfi.py` | fund name/category → NAV, comparison | 30-min in-memory TTL. |
| `pdf_scraper.py` | symbol/doc_type/period → text | Postgres `pdf_cache`, max 40 pages via `pdfplumber`. |
| `technical.py` | OHLCV → SMA/EMA/BB/RSI/MACD/ATR/Stochastic/ADX/VWAP/Supertrend | `ta` lib or hand-rolled fallback; ≥30 rows required. |
| `fundamental.py` | raw statements → ratios + DCF + citations | Recomputes from raw statements (yfinance D/E inconsistency); now footnote-tagged. |
| `risk.py` | multi-factor inputs → 0-100 score | Weighted, thresholds from `config.py`. |
| `sentiment.py` | news → score/label | VADER + finance-domain word boosts, fallback keyword counting. |
| `backtester.py` | OHLCV, params → CAGR/vol/Sharpe/max-drawdown | Simple long-only, no external lib. |

---

## 7. Data Layer

**Postgres (Neon)**, `psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=10)`. Tables: `users`, `sessions`, `portfolio`, `watchlist`, `alerts` (14 alert types, fires once then deactivates), `cache_meta` (declared, effectively unused), `chat_sessions`, `chat_history`, `saved_screeners`, `reports` (dual-written to disk as `.md`/`.pdf`), `pdf_cache` (tenant-agnostic), `financial_profiles` (JSONB, PlanningAgent).

**User memory**: in-process `LLMClient` history capped at last 20 messages per call; full transcript persisted separately in `chat_history` for session resumption.

**Caching**: OHLCV Parquet (4h), quotes/financials in-memory (5min), bhavcopy (daily), AMFI NAV (30min), PDF filings (Postgres, effectively permanent).

---

## 8. LLM Stack

| Task | Model | Why |
|---|---|---|
| Primary synthesis | `llama-3.3-70b-versatile` (Groq) | Free-tier-friendly, fast, strong enough with grounding data supplied. |
| Fallback on 429 (pre-first-chunk only) | `gemini-2.0-flash` | Keeps app usable without a paid backup; mid-stream now degrades gracefully instead of silently switching. |
| Reserved, unused | `gemma2-9b-it` | Defined in config, not wired into any agent call on `main`. |

Key rotation: `_KeyRotator` round-robins up to 5 Groq keys, thread-safe, shared across all agents/users in-process. Token budgets: `TOKENS_SYNTHESIS=2000`, `TOKENS_COMPLEX=3000`, `TOKENS_AGENT=800`, `TOKENS_FAST=200`, `LLM_TEMPERATURE=0.3`, `MAX_RETRIES=2` (3 total attempts, exponential backoff).

---

## 9. Context Engineering

- **Conversation history**: capped at last 20 messages sent to the model per call; full history persists in Postgres for resumption.
- **Summarization**: none — older messages are dropped, not summarized, past the 20-message window.
- **Retrieval**: no vector store; all "retrieval" is deterministic structured-data fetching triggered by keyword/entity matching.
- **Context compression**: EquityAgent's data-confidence tiering and citation-marker system is the closest analog — it structures the *current-turn* data bundle densely (footnoted, capped-length excerpts) rather than compressing history.

---

## 10. Evaluation

No automated test suite, benchmark, or eval harness on `main`. `CLAUDE.md` states this explicitly. Correctness is verified manually via the UI plus structural safeguards (`validate_ticker()` re-checks LLM-returned tickers, `compute_rr()` rejects malformed risk/reward inputs, fundamentals recomputed from raw statements rather than trusted from yfinance).

**Hallucination mitigation** (most developed post-merge):
- `_grounding_note()` — post-hoc regex cross-check of numeric claims vs. the data block, now citation-aware (exempts `[^key]`-marked numbers).
- Citation markers trace a stated ratio back to its computed source.
- `DATA CONFIDENCE` line forces an explicit acknowledgment of data completeness before a verdict.
- None of this is tested against a labeled/golden-output dataset — still runtime heuristics, not measured accuracy.

---

## 11. Future Roadmap (open items relevant to `main`)

- Extend `[^key]` citation markers to technical-indicator figures (RSI, MACD, pivot levels) and risk-score components — currently fundamentals-only.
- Consider function-calling / structured output so the model can only emit a number by referencing a field path, closing the "still didn't cite" gap.
- Track citation-rate as a metric over time.
- `CLAUDE.md` needs a real update pass (4-tab/SQLite/4-agent claims are all stale).
- No automated tests — acceptable at current scale, increasingly risky as the agent/tool surface grows.
- `GROQ_MODEL_FAST` defined but unused — either wire it into a genuine fast-path or remove.
- `cache_meta` table unused by any query path.
- The much larger `langgraph` branch (see `ARIA_LANGGRAPH_WORKFLOW_REPORT.md`) is a separate, unmerged experimental rewrite — not part of `main`'s current architecture; any future merge decision there should be made deliberately, not by default drift.

---

*Compiled from `main` post-merge (`git show main:...` reads of `app.py`, `agents/*.py`, `tools/**/*.py`, `config.py`, `utils/db.py`, `utils/helpers.py`, `ARCHITECTURE.md`, `CLAUDE.md`, `requirements.txt`), cross-referenced with the pre-merge audit in `ARIA_ARCHITECTURE_REPORT.md`.*
