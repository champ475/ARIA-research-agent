# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ARIA v2** (Autonomous Research & Investment Analyst) is a Python multi-agent investment research platform for Indian equity markets (NSE/BSE). It provides AI-powered stock analysis, institutional-grade report generation, mutual fund comparison, stock screening, portfolio/alert management, and goal-based financial planning via a Gradio chat UI.

## Setup & Running

```bash
# Install dependencies
pip install -r requirements.txt

# Configure API keys (copy and edit)
cp .env.example .env
# Set GROQ_API_KEY_1, GEMINI_API_KEY, SECRET_KEY (required); GROQ_API_KEY_2/3 optional for higher rate limits
# SECRET_KEY: any random string (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`) —
# encrypts BYO LLM keys (Settings tab) before they're stored; without it, saving a key fails.

# Create/upgrade the database schema, then run the app
python scripts/migrate.py
python app.py
# Opens at http://localhost:7860
```

No build step required. Tests live under `tests/` (unit tests for `tools/`, `graph/`, `agents/`, `utils/`, plus `tests/integration/` on a real Postgres). UI-level flows are still verified manually via Gradio.

```bash
pip install -r requirements-dev.txt     # app deps + ruff, mypy, pytest-cov, hypothesis, testcontainers, bandit, pip-audit
python -m pytest tests/ -q              # integration tests use TEST_DATABASE_URL, else a Docker Postgres, else skip
ruff check . && ruff format --check .   # lint + format (ruff pinned to 0.17.0)
mypy                                    # type check (legacy modules ratcheted in pyproject.toml)
pre-commit install                      # optional: run the same gates on every commit
```

CI (`.github/workflows/ci.yml`) runs lint, types, tests with coverage (floor in `pyproject.toml`, against a Postgres service container) and security (bandit, pip-audit, gitleaks). Never point tests at the `.env` `DATABASE_URL`.

## Architecture

### Entry Point & UI
`app.py` builds a 3-tab Gradio interface (Chat, Watchlist, Settings — BYO LLM key, Telegram linking, alert inbox) with an auth gate (`utils/auth.py`). All specialist domains (equity, reports, mutual funds, screener, alerts, planning) are routed through the single Chat tab via the LangGraph research engine (`graph/`), not per-domain tabs. One `GraphOrchestrator` (`agents/graph_orchestrator.py`) is lazily initialized per user session (`get_orchestrator(user_id)`), each with its own conversation memory.

### Orchestration (`agents/graph_orchestrator.py`, `graph/`)
A LangGraph `StateGraph` (see `docs/RFC_AGENTIC_RESEARCH_GRAPH.md`) is the sole orchestration path: QueryAnalyzer (intent/entity/complexity) → Planner (LLM-driven tool selection over `graph/tool_catalogue.py`) → parallel ResearchExecutor/execute_task fan-out → EvidenceSummarizer → Reflection/Verification self-correction loop (with human-in-the-loop gates) → ConclusionBuilder → CommunicationLayer (the only node allowed to call the primary LLM). Equity/chat turns stream token-by-token (`GraphOrchestrator` makes the deferred LLM call itself, see `communication_layer.py::_is_streamable`); report/mf/screener/alert/planning intents return their finished text in one yield.

### LLM Provider (`agents/llm_client.py`)
`LLMClient` wraps Groq and Gemini providers with:
- Thread-safe round-robin key rotation across up to 5 Groq API keys (enables scaling free tier)
- Automatic fallback to Gemini 2.0 Flash on Groq 429 errors
- Per-instance conversation history (capped at 20 messages)
- Optional last-resort local LLM (`agents/providers/local_llm.py`) when `LOCAL_LLM_BASE_URL` is set — any OpenAI-compatible server (e.g. llama.cpp from `..\local-llm`, `.\start-server.ps1` → `http://127.0.0.1:8080/v1`). Tried after Groq/Gemini (or a BYO key) fail, once per call, output capped by `LOCAL_LLM_MAX_TOKENS`. Tests disable it via an autouse fixture in `tests/conftest.py`.

Primary model: `llama-3.3-70b-versatile`. Fast model: `llama-3.1-8b-instant`. Fallback: `gemini-2.0-flash`.

### Tools Layer (`tools/`)
- `tools/data/` — Data fetchers: `fetcher.py` (yfinance wrapper), `nse_scraper.py` (bhavcopy, bulk/block deals), `news_fetcher.py` (RSS + Google News), `amfi.py` (mutual fund NAV), `cache.py` (Parquet caching to reduce API calls)
- `tools/analysis/` — Compute indicators: `technical.py` (RSI, MACD, Bollinger Bands), `fundamental.py` (PE, PB, ROE, ROCE), `risk.py` (volatility, beta), `backtester.py` (MA crossover, RSI strategies), `sentiment.py`
- `tools/charts/builder.py` — Plotly candlestick charts and heatmaps

### Configuration (`config.py`)
Central source of truth for: LLM token budgets (`TOKENS_SYNTHESIS=2000`, `TOKENS_COMPLEX=3000`, `TOKENS_AGENT=800`, `TOKENS_FAST=200`), sector universe (~100 NSE stocks across 9 sectors, `NIFTY200`/`SECTOR_UNIVERSE`), technical analysis defaults (RSI 14, MACD 12/26/9, BB 20), risk thresholds, and data source URLs.

### Persistence (`utils/db.py`)
Postgres (Neon), via `psycopg2.pool.ThreadedConnectionPool`. Tables: `users`, `sessions`, `portfolio`, `watchlist`, `alerts`, `notifications`, `chat_sessions`, `chat_history`, `saved_screeners`, `reports`, `pdf_cache`, `financial_profiles`, `evidence_cache`, `user_llm_config`, `telegram_link_codes`, `auth_attempts`, plus LangGraph's `checkpoint*` tables. **Schema = Alembic migrations** (`migrations/versions/`, ARCH-01): add a new revision for any schema change (never DDL in app code); `python scripts/migrate.py` applies them (Docker runs it before the app); startup only checks the version (`utils/db.py::ensure_schema`). A leftover `data/aria.db` SQLite file exists in-repo but is unread by current code.

### Ticker Resolution (VAL-02: `tools/data/instruments.py`, `graph/entity_resolution.py`)
Every company mention resolves against the vendored NSE/BSE instrument master (`data/instruments/`, refresh with `scripts/refresh_instruments.py`) to found / ambiguous / not_found — never trust a guessed or LLM-produced ticker. Aliases (`"SBI"` → `"SBIN"`) live in `utils/helpers.py::TICKER_ALIASES`; renamed symbols in `instruments.RENAMED`. `config.STOCK_UNIVERSE` and alias targets must be listed symbols (a test enforces it). All tickers must be suffixed with `.NS` for NSE or `.BO` for BSE when passed to yfinance.

## Production-readiness contracts (Phase 0, see ARCHITECTURE.md)

- **Logging**: use `logging.getLogger(__name__)`, never `print()` in app code (ruff T20 + a test enforce it). `utils/logging_setup.py` emits JSON with a per-turn `turn_id`; chat turns run inside `run_in_turn`.
- **Evidence cache**: a tool in `graph/tool_catalogue.py` may be `cacheable` only if `entity + cache_key(...)` covers every input that changes its output; write tools are `side_effect=True, cacheable=False` (invariant tests in `tests/graph/test_cache_contract.py`).
- **Write tools**: run only when `graph/write_policy.py::write_allowed` passes (intent owns the tool AND the user's message has an explicit verb). Wrap third-party text in prompts with `graph/prompt_safety.py::wrap_untrusted`.
- **User input**: chat text goes through `utils/input_guard.py::clean_message` first; user-supplied URLs through `utils/url_safety.py`.
- **Tenant isolation**: every new `utils/db.py` function taking `user_id` needs a case in `tests/integration/test_tenant_isolation.py::COVERED` (a meta-test fails otherwise).
- **External calls** (REL-02): HTTP goes through `utils/http.py` (default timeouts, retry with jitter, per-source circuit breaker); libraries without a timeout (yfinance) go through `utils/resilience.py::guarded_call`. Never add a bare `requests.get` / `yf.Ticker(...).info` in app code. Each turn has a deadline (`graph/deadline.py`): research stops at 70% of `TURN_DEADLINE_SECONDS` and the answer names missing data.
- **Tracing** (OBS-02): each turn runs inside `utils/tracing.turn` (opened by `GraphOrchestrator.process_stream`) and writes one `turns` row; LLM calls, tools and graph nodes are counted/spanned automatically. Only `utils/tracing.py` talks to LangSmith, and only redacted data (`redact_for_trace`). User-facing errors show `Error ID` = turn id prefix (`scripts/find_turn.py`).
- **Advice boundary** (COMP-01 / LLM-09): education-only (`docs/ADVICE_POLICY.md`). Requests go through `graph/guardrails.py::check_input` before the graph (refuse / reframe); every answer goes through `finalize` (removes promise / buy-sell-call sentences, appends the footer from `graph/advice_policy.py`). Never ask the LLM to add disclaimers, and never prompt for personal picks.
- **Schema** (ARCH-01): schema changes are new Alembic revisions in `migrations/versions/`; app code never runs DDL.
- **Concurrency**: chat turns run concurrently (`CHAT_CONCURRENCY`), one per user (`respond_exclusive`); module-level mutable state must be thread-safe.

## Changelog

Log at `C:\Users\arnav\Desktop\Arnav\PROJECTS\CHANGELOG_ARIA.md` (outside repo, survives branch switches).

**Update it for every major update, feature change, or fix.** New entry per change, newest on top, format:

```
## YYYY-MM-DD HH:MM IST — branch: `<branch-name>`
### <Title>
<Description — what changed, why, files touched>
```

Get branch via `git branch --show-current`, timestamp IST (`pytz` Asia/Kolkata). Skip trivial edits (typos, formatting, comments).

## Architecture Decision Log

Log at `ARCHITECTURE.md` (in-repo — tracks the codebase it documents).

**Add an entry for every significant architectural decision**: a change to a
system boundary, data flow, persistence model, provider/library choice, or a
trust/reliability contract with the user. Routine feature work and bug fixes
go in the changelog above instead. Each entry, newest on top:

```
## YYYY-MM-DD — <Title>
**Problem.** <what forced a decision>
**Alternatives considered.** <options weighed, and why each was passed over>
**Final decision.** <what was built>
**Trade-offs.** <what was given up / what risk remains>
**Future improvements.** <what would close the gap later>
```

If a later change reverses or supersedes an entry, add a new entry rather
than editing the old one — the log is a history, not a snapshot.

## Key Constraints

- **Token budgets**: Keep LLM calls within limits defined in `config.py`. Use `TOKENS_FAST` for suggestions, `TOKENS_AGENT` for sub-agent calls, `TOKENS_SYNTHESIS` for full analysis.
- **Rate limits**: Groq free tier is 14,400 req/day per key. The `_KeyRotator` in `llm_client.py` handles this — don't bypass it.
- **Data caching**: Use `tools/data/cache.py` for OHLCV data to avoid yfinance rate limits. Cache is Parquet format keyed by `{symbol}_{period}_{interval}`.
- **IST timezone**: All timestamps must use `pytz` Asia/Kolkata timezone (see `utils/helpers.py`).
