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
# Set DATABASE_URL, SECRET_KEY (required, >= 32 chars: `python -c "import secrets; print(secrets.token_urlsafe(32))"`),
# GROQ_API_KEY_1 / GEMINI_API_KEY (the free trial). Typed + validated at boot by config.py (ARCH-03):
# a bad value exits with one readable list of problems. APP_ENV = dev | test | staging | prod.

# Create/upgrade the database schema, then run the app
python scripts/migrate.py
python app.py        # uvicorn on server.py: FastAPI (login/signup, /healthz, /readyz, /metrics,
                     # /health/data, /download/...) with the Gradio UI mounted at /
python worker.py     # background jobs (alerts, Telegram, purges, snapshots, canary, watchdog);
                     # in dev the app runs them in-process (RUN_JOBS_IN_APP)
# Opens at http://localhost:7860 (redirects to /login)
```

Offline and deterministic: `ARIA_FAKE_LLM=1 ARIA_FAKE_DATA=1` (fake LLM per graph node, recorded data from `data/fake/`; refused when `APP_ENV=prod`); `ARIA_NO_DOTENV=1` ignores `.env`. `docker compose -f compose.test.yml up` runs that stack.

No build step required. Tests live under `tests/`: unit tests for `tools/`, `graph/`, `agents/`, `utils/`, `services/`, `web/`; `tests/integration/` on a real Postgres; `tests/edge_cases/` (YAML matrix + Hypothesis); `tests/e2e/` (Playwright journeys + axe, opt-in).

```bash
pip install -r requirements-dev.txt     # app deps + ruff, mypy, pytest-cov, hypothesis, testcontainers, bandit, pip-audit
python -m pytest tests/ -q              # integration tests use TEST_DATABASE_URL, else a Docker Postgres, else skip
ruff check . && ruff format --check .   # lint + format (ruff pinned to 0.17.0)
mypy                                    # type check (legacy modules ratcheted in pyproject.toml)
pre-commit install                      # optional: run the same gates on every commit
E2E=1 python -m pytest tests/e2e        # browser journeys (first: python -m playwright install chromium)
lint-imports                            # layering: ui/ calls services/, never utils.db or tools (import-linter, ARCH-02)
deptry . && vulture                     # no unused or undeclared dependency, no dead code (ARCH-07)
python loadtests/run.py --database-url <throwaway db> ...   # load + chaos report (TEST-06, loadtests/README.md)
make lock                               # after editing requirements*.txt (hashed locks, SEC-14)
```

CI (`.github/workflows/ci.yml`) runs lint, types, tests with coverage (floor in `pyproject.toml`, against a Postgres service container), e2e, eval, a lock drift check, the image build + Trivy scan, and security (bandit, pip-audit, gitleaks). Releases: `docs/RELEASING.md` (`release.yml` on `v*.*.*` tags; only the owner pushes tags). Never point tests at the `.env` `DATABASE_URL`, and never run `scripts/migrate.py` against it from a dev session.

## Architecture

### Entry Point & UI
`server.py` is the ASGI app: FastAPI routes in `web/` (server-rendered auth pages, account actions, downloads, ops endpoints) with the Gradio UI from `app.py` mounted at `/`. A security-headers middleware (`web/security_headers.py`, SEC-15) covers every response. Auth is an HttpOnly `aria_session` cookie whose sha256 is stored in `sessions` (SEC-04); Gradio handlers get the user from the request, never from `gr.State`.

The UI is layered (ARCH-02). `app.py` (under 150 lines) only composes the page and runs start-up. `ui/` has one module per tab (`chat`, `watchlist`, `portfolio`, `alerts`, `settings`, `admin`, which is hidden unless `users.is_admin`), plus `theme.py` and `theme.css`; it renders and wires Gradio events and imports neither `utils.db` nor `tools` (an import-linter contract in CI). `services/` holds what the tabs need (`chat`, `watchlist`, `portfolio`, `alerts`, `settings`, `admin`): no Gradio, and the only place the UI's database and data calls live. Chat history has rename, full-text search, paging and Markdown export (UX-09); answers carry a collapsible Sources block (`graph/provenance.py`, UX-07). All specialist domains (equity, reports, mutual funds, screener, alerts, planning, portfolio) are routed through the single Chat tab via the LangGraph research engine (`graph/`). One `GraphOrchestrator` (`agents/graph_orchestrator.py`) per (user, chat session), in a bounded TTL cache rebuilt from `chat_history` on a miss (REL-03, VAL-10; `services/chat.py`). `worker.py` runs scheduled jobs, each under a Postgres advisory lock with a `job_heartbeats` row (REL-05), including the nightly `retention` job.

### Orchestration (`agents/graph_orchestrator.py`, `graph/`)
A LangGraph `StateGraph` (see `docs/RFC_AGENTIC_RESEARCH_GRAPH.md`) is the sole orchestration path: QueryAnalyzer (intent/entity/complexity) → Planner (LLM-driven tool selection over `graph/tool_catalogue.py`) → parallel ResearchExecutor/execute_task fan-out → EvidenceSummarizer → Reflection/Verification self-correction loop (with human-in-the-loop gates) → ConclusionBuilder → CommunicationLayer (the only node allowed to call the primary LLM; it only renders: no database, no files) → OutputFiles (saves a report, stores a screener CSV). Two intents skip research and end their turn right after QueryAnalyzer (ARCH-05): planning (`PlanningNode`) and "show/download my reports" (`ReportManagementNode`). Checkpoints live in memory (`graph/checkpointer.py::TurnCheckpointer`); only a turn paused on a yes/no is copied to Postgres (ARCH-04). Equity/chat turns stream token-by-token (`GraphOrchestrator` makes the deferred LLM call itself, see `communication_layer.py::_is_streamable`); report/mf/screener/alert/planning intents return their finished text in one yield.

### LLM Provider (`agents/llm_client.py`)
`LLMClient(node=...)` wraps Groq and Gemini providers with:
- Per-node model routing (LLM-08): `config.MODEL_ROUTING` maps each node to an ordered `provider:model` list ("fast" or "synthesis"); override one node with `ARIA_MODEL_<NODE>="groq:...,gemini:..."`. A background startup probe marks dead models unhealthy (`agents/model_health.py`).
- Thread-safe round-robin key rotation across up to 5 Groq API keys (enables scaling free tier)
- Fallback down the route on 429s / errors, then to a BYO key's provider
- Reasoning models (gpt-oss): `_groq_params` adds low reasoning effort and `LLM_REASONING_HEADROOM` tokens to every call, and an empty reply raises `EmptyReply` (retried once with double the budget, then the next provider; counted as `aria_llm_empty_replies`). Never pass the bare cap to a Groq call
- Metering (LLM-11): tokens, latency, model and ₹ cost per call (`config.MODEL_PRICES_USD_PER_M`) land on the turn's `turns` row; trial users also have a daily token cap
- Per-instance conversation history (capped at 20 messages)
- `ARIA_FAKE_LLM=1` swaps in `agents/providers/fake_llm.py` (tests, demos)
- Optional last-resort local LLM (`agents/providers/local_llm.py`) when `LOCAL_LLM_BASE_URL` is set — any OpenAI-compatible server (e.g. llama.cpp from `..\local-llm`, `.\start-server.ps1` → `http://127.0.0.1:8080/v1`). Tried after Groq/Gemini (or a BYO key) fail, once per call, output capped by `LOCAL_LLM_MAX_TOKENS`. Tests disable it via an autouse fixture in `tests/conftest.py`.

Defaults: synthesis `openai/gpt-oss-120b` (Groq), fast `openai/gpt-oss-20b` (Groq), Gemini `gemini-3.6-flash` / `gemini-3.5-flash-lite`. (`llama-3.3-70b-versatile`, `llama-3.1-8b-instant` and `gemini-2.0-flash` are retired, and `gemini-2.5-*` is closed to new keys; recorded eval fixtures still use them until re-recorded.)

### Tools Layer (`tools/`)
- `tools/data/` — Data fetchers behind one interface, `providers/market_data()` (DATA-02: yfinance + NSE, or `FakeProvider` with `ARIA_FAKE_DATA`): `fetcher.py` (yfinance wrapper), `nse_scraper.py` (bhavcopy, large deals, corporate actions), `news_fetcher.py` (RSS + Google News), `amfi.py` / `mf_data.py` (fund NAVs), `cache.py` (Parquet caching), `contracts.py` (pydantic schemas each source must match, TEST-05). Every source is listed in `docs/DATA_SOURCES.md` (a test enforces it).
- `tools/analysis/` — `technical.py` (RSI, MACD, Bollinger Bands), `fundamental.py` (PE, PB, ROE, ROCE; `sector_profile.py` suppresses ROCE/D-E for lenders), `risk.py`, `backtester.py`, `sentiment.py`, wired as catalogue tools in `graph/analysis_tools.py` (UX-04)
- `tools/screener_grammar.py` (VAL-06) parses filters; the screener runs SQL over the `fundamentals_snapshot` table (DATA-05, refreshed nightly by the worker)
- `tools/portfolio.py` — holdings, CSV import, P&L (UX-05)
- `tools/charts/builder.py` — Plotly charts; `chart_text_summary` is each price chart's text alternative (UX-03)

### Configuration (`config.py`)
Central source of truth for: LLM token budgets (`TOKENS_SYNTHESIS=2000`, `TOKENS_COMPLEX=3000`, `TOKENS_AGENT=800`, `TOKENS_FAST=200`), sector universe (~100 NSE stocks across 9 sectors, `NIFTY200`/`SECTOR_UNIVERSE`), technical analysis defaults (RSI 14, MACD 12/26/9, BB 20), risk thresholds, and data source URLs.

### Persistence (`utils/db.py`)
Postgres (Neon), via psycopg 3 and one shared `psycopg_pool.ConnectionPool` (REL-04; 5 s statement timeout). Tables: `users`, `sessions`, `auth_tokens`, `auth_attempts`, `terms_versions`, `audit_log` (append-only), `portfolio`, `watchlist`, `alerts`, `notifications`, `chat_sessions`, `chat_history`, `answer_feedback`, `saved_screeners`, `reports`, `pdf_cache`, `downloads`, `financial_profiles`, `evidence_cache`, `user_llm_config`, `telegram_link_codes`, `turns`, `mf_metrics`, `fundamentals_snapshot`, `job_heartbeats`, `ops_alerts`, `ohlcv_cache` and `bhavcopy_bars` (shared market data, DATA-10), `feature_flags` (OBS-05), plus LangGraph's `checkpoint*` tables (msgpack only, no pickle — SEC-09). **Schema = Alembic migrations** (`migrations/versions/`, ARCH-01): add a new revision for any schema change (never DDL in app code); `python scripts/migrate.py` applies them (Docker runs it before the app); startup only checks the version (`utils/db.py::ensure_schema`).

### Ticker Resolution (VAL-02: `tools/data/instruments.py`, `graph/entity_resolution.py`)
Every company mention resolves against the vendored NSE/BSE instrument master (`data/instruments/`, refresh with `scripts/refresh_instruments.py`) to found / ambiguous / not_found — never trust a guessed or LLM-produced ticker. Aliases (`"SBI"` → `"SBIN"`) live in `utils/helpers.py::TICKER_ALIASES`; renamed symbols in `instruments.RENAMED`. `config.STOCK_UNIVERSE` and alias targets must be listed symbols (a test enforces it). All tickers must be suffixed with `.NS` for NSE or `.BO` for BSE when passed to yfinance.

## Roadmap

Work follows the production-readiness PRD, which is a living Claude Doc:
https://claude.ai/code/artifact/9bae50e1-07a3-44c1-a152-133336d4b915.

- An offline snapshot is at `docs/PRD_PRODUCTION_READINESS.md`. Re-export it from the doc if the doc has changed.
- The PRD's "Roadmap and release gates" section lists the items for each phase, by ID (SEC-, VAL-, LLM-, DATA-, REL-, TEST-, OBS-, ARCH-, UX-, COMP-, OPS-).
- Phases 0 and 1 are done (PR #1, PR #2). Phase 2 is done on branch `phase2-production-ready` (PR #3); deviations are listed in the PR and the PRD's Phase 2 status note. Hosting (OPS-03) is skipped by the owner's decision: docker-compose is the reference deployment.
- Phase 3 is done on branch `phase3-scale-polish` (20 items, one commit each): see the PRD's Phase 3 status note. Migrations 0022–0027 are new and not yet applied to Neon (steps in the owner TODO file named above). Cost model and capacity plan: `docs/CAPACITY.md`; load results: `docs/load/`.
- The live smoke test (2026-10-17, re-run 2026-10-18) found and fixed the issues listed in `docs/smoke/2026-10-17-live-smoke.md` (branch `smoke-test-fixes`, no new migrations). Evals were re-recorded; the only open items are listed there.
- The UI redesign smoke test (2026-10-18, branch `ui-redesign-night-ledger`) is in `docs/smoke/2026-10-18-ui-redesign-smoke.md`: UI bugs it found are fixed; its errors, latency, research-graph and content findings are the open backlog.
- Tasks only the repo owner can do (keys, migrations on production, GitHub settings, sign-offs) are in `C:\Users\arnav\Desktop\Arnav\PROJECTS\TODO_ARIA.md`, which lives outside the repo.
- `docs/archive/PRD.md` is the older July product PRD and is not the current plan.
- Operating docs: `docs/RUNBOOK.md` (one section per ops alert), `docs/SLO.md`, `docs/ENVIRONMENTS.md`, `docs/RELEASING.md`, `docs/DATA_SOURCES.md`, `docs/PRIVACY.md`, `docs/ADVICE_POLICY.md`, `docs/SECURITY.md`.

## Production-readiness contracts (Phases 0–2, see ARCHITECTURE.md)

- **Logging**: use `logging.getLogger(__name__)`, never `print()` in app code (ruff T20 + a test enforce it). `utils/logging_setup.py` emits JSON with a per-turn `turn_id`; chat turns run inside `run_in_turn`.
- **Evidence cache**: a tool in `graph/tool_catalogue.py` may be `cacheable` only if `entity + cache_key(...)` covers every input that changes its output; write tools are `side_effect=True, cacheable=False` (invariant tests in `tests/graph/test_cache_contract.py`).
- **Write tools**: run only when `graph/write_policy.py::write_allowed` passes (intent owns the tool AND the user's message has an explicit verb). Wrap third-party text in prompts with `graph/prompt_safety.py::wrap_untrusted`.
- **User input**: chat text goes through `utils/input_guard.py::clean_message` first; user-supplied URLs through `utils/url_safety.py`.
- **Tenant isolation**: every new `utils/db.py` function taking `user_id` needs a case in `tests/integration/test_tenant_isolation.py::COVERED` (a meta-test fails otherwise).
- **External calls** (REL-02): HTTP goes through `utils/http.py` (default timeouts, retry with jitter, per-source circuit breaker); libraries without a timeout (yfinance) go through `utils/resilience.py::guarded_call`. Never add a bare `requests.get` / `yf.Ticker(...).info` in app code. Each turn has a deadline (`graph/deadline.py`): research stops at 70% of `TURN_DEADLINE_SECONDS` and the answer names missing data.
- **Tracing** (OBS-02): each turn runs inside `utils/tracing.turn` (opened by `GraphOrchestrator.process_stream`) and writes one `turns` row; LLM calls, tools and graph nodes are counted/spanned automatically. Only `utils/tracing.py` talks to LangSmith, and only redacted data (`redact_for_trace`). User-facing errors show `Error ID` = turn id prefix (`scripts/find_turn.py`).
- **Verification severity** (smoke-test fixes): deterministic and semantic issues are `hard` (evidence withheld, the yes/no gate asks, naming the claim); LLM-judge issues are `soft` (answer shown with a note from `graph/caveats.py`). An unusable judge reply is `judge_status="unavailable"`, never "clean".
- **No company, no tools**: company-bound tools (`graph/analysis_tools.py::COMPANY_BOUND_TOOLS`) are never planned without a company; the turn asks which one (`missing_company`). Tools that read the user's words (`_query`, fund names) must key their cache on them (`tests/graph/test_cache_contract.py`).
- **Advice boundary** (COMP-01 / LLM-09): education-only (`docs/ADVICE_POLICY.md`). Requests go through `graph/guardrails.py::check_input` before the graph (refuse / reframe); every answer goes through `finalize` (removes promise / buy-sell-call sentences, appends the footer from `graph/advice_policy.py`). Never ask the LLM to add disclaimers, and never prompt for personal picks.
- **Evaluation** (LLM-01): `make eval` (or `python evals/runner.py --replay --compare evals/baseline.json`) must stay green; CI blocks a >2-point drop. Changing a node prompt invalidates its recorded fixtures — re-record with `python evals/runner.py --record`, then `make eval-baseline`. Add golden cases in `evals/build_golden.py` and re-run it.
- **Schema** (ARCH-01): schema changes are new Alembic revisions in `migrations/versions/`; app code never runs DDL.
- **Concurrency**: chat turns run concurrently (`CHAT_CONCURRENCY`), one per user (`respond_exclusive`); module-level mutable state must be thread-safe.
- **Config** (ARCH-03): add settings to `config.Settings` (typed, validated at boot), never `os.getenv` in app code.
- **Errors** (SEC-08): raise `utils/errors.py` types (`UserInputError`, `DataUnavailableError`, `ProviderError`, …) with a `user_message`; boundaries render "Something went wrong (ref …)". Never put `str(e)` / `{e}` in user-facing text (a test greps for it).
- **Sessions and audit** (SEC-04, COMP-04): web routes get the user from `web/session.py`; security-relevant actions call `utils/audit.py::audit(...)`.
- **Prompts** (LLM-07): prompt text lives in `prompts/<name>.vN.md` and is loaded with `prompts.get(name)`; no inline `_SYSTEM` / `_PROMPT` strings elsewhere (a test enforces it). A new prompt version needs re-recorded eval fixtures.
- **Structured output** (LLM-05): parse model JSON with `utils/llm_json.py::parse_model(raw, PydanticModel)` (one repair retry, counted as `parse_failures`).
- **Market time** (DATA-06): open/closed and "as of" come from `utils/market_clock.py` (vendored NSE holidays in `data/market/nse_holidays.csv`, refresh yearly).
- **Background jobs** (REL-05): add a job to `worker.py`'s list; it gets an advisory lock and a heartbeat. Never start a scheduler thread in app code.
- **Downloads** (OPS-02): files are generated from Postgres on request (`web/download_routes.py`), never written to a replica's disk; answers link with Markdown (`services/chat.py::download_links`).
- **Ops** (OBS-03/04): new metrics go in `utils/metrics.py`; new ops alerts in `utils/watchdog.py`, each with a `docs/RUNBOOK.md` section.
- **Data sources** (COMP-05): a new outside source needs a row in `docs/DATA_SOURCES.md`.
- **UI** (UX-03): colours come from the `--aria-*` tokens in `ui/theme.css` (`ui.theme.APP_CSS`) (AA contrast checked by `tests/test_a11y_tokens.py`), text ≥ 12px; dark is the default (light only as a stored choice); blue (`--aria-accent*`) only for live or actionable things, `--aria-gain`/`--aria-loss` only for price moves; icons come from `ui/icons.py` (`elem_classes=["aria-ico", "ico-NAME"]`, never emoji in labels); tab icons are assigned by position in `theme.css`; events that write the chatbot use `show_progress="hidden"`; axe must stay at 0 serious/critical (`tests/e2e/test_a11y.py`); no sideways scroll at 375/768/1280 px (`tests/e2e/test_mobile.py`, UX-08).
- **Layering** (ARCH-02): UI modules never import `utils.db` or `tools`; add a function to `services/` and call that. `services/` never imports Gradio. `lint-imports` enforces both.
- **Admin** (OBS-05): every `services/admin.py` function starts with `require_admin(actor)` (403 otherwise) and audits what it does; a new one must be added to `CALLS` in `tests/integration/test_admin_console.py`. Runtime switches are `utils/flags.py` flags, never env edits.
- **Retention** (COMP-06, DATA-10): a table that grows needs a rule in `utils/retention.py::schedule()` and a row in `docs/PRIVACY.md` section 4. Money and quantities are `NUMERIC(18,4)` and `Decimal` (`utils/money.py`).
- **Passwords** (SEC-07): argon2id, 8–128 characters, common passwords refused; old PBKDF2 hashes upgrade on login. Never log or compare a password outside `utils/auth.py`.
- **Prompts that gate** (LLM-13): the verification judge is scored by `python evals/judge_calibration.py` (precision ≥ 0.8, recall ≥ 0.7, gate rate < 5%); a prompt change means re-recording `evals/fixtures/judge.jsonl`.
- **Load tooling** (TEST-06): the fake LLM and fake data read `ARIA_FAULTS_FILE` (`utils/faults.py`) for injected latency and failures; nothing else may.
- **Migrations** (TEST-07): expand/contract — a destructive upgrade must say `CONTRACT:` in its docstring and ship after the code stopped using the old shape.

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
