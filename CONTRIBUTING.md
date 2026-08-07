# Contributing to ARIA

## Dev setup

```bash
git clone <your-fork-url>
cd ARIA_investment_agent
pip install -r requirements.txt
cp .env.example .env   # fill in GROQ_API_KEY_1, GEMINI_API_KEY, DATABASE_URL
python app.py
```

Or skip the Postgres signup and use `docker compose up --build` (see README).

## Running tests

```bash
python -m pytest tests/ -q
```

Tests that touch a live database are skipped unless `DATABASE_URL` is set
and `RUN_DB_TESTS=1` — CI runs without either, so those are opt-in locally.

## Project layout

- `agents/` — one file per specialist (equity, report, mf, screener, alert,
  planning) plus `orchestrator.py` (keyword routing) and
  `graph_orchestrator.py` (LangGraph adapter, behind
  `USE_LANGGRAPH_ORCHESTRATOR`). Each agent returns `(markdown_text,
  chart_html)`.
- `tools/data/` — external data fetchers (yfinance, NSE bhavcopy, news,
  AMFI NAV) and the Parquet cache.
- `tools/analysis/` — pure computation: technicals, fundamentals, risk,
  backtesting, sentiment.
- `graph/` — the experimental LangGraph research engine (see
  `docs/RFC_AGENTIC_RESEARCH_GRAPH.md`).
- `utils/` — DB access, auth, ticker-alias resolution, IST time helpers.
- `config.py` — single source of truth for token budgets, sector universe,
  and technical-analysis defaults. Change limits here, not inline in agents.

## Adding a new agent or tool

1. New data fetcher → `tools/data/`; new computation → `tools/analysis/`.
   Keep tools pure (input → output), no LLM calls inside them.
2. New specialist agent → subclass the pattern in an existing agent
   (`agents/screener_agent.py` is a good small example): parse intent →
   call tools → synthesize via `LLMClient` → return `(text, chart_html)`.
3. Wire routing keywords into `agents/orchestrator.py::route()`. If the
   LangGraph path is enabled, also register the tool in
   `graph/tool_catalogue.py`.
4. Respect the token budgets in `config.py` (`TOKENS_FAST` /
   `TOKENS_AGENT` / `TOKENS_SYNTHESIS`) — don't hardcode a `max_tokens`.
5. All timestamps use `pytz` Asia/Kolkata (`utils/helpers.py` has the
   helper) — NSE market-hours logic depends on this being consistent.

## Architecture decisions

Significant decisions (system boundaries, persistence model, provider
choice) are logged in `ARCHITECTURE.md`, newest entry first. If your PR
changes one of those, add an entry rather than editing history.

## Code style

No enforced formatter/linter is wired up yet — match the surrounding
file's style. Keep functions small and prefer explicit code over new
abstractions for one-off logic.

## Pull requests

- Keep PRs scoped to one change; unrelated cleanups belong in a separate PR.
- Run the test suite before opening a PR.
- Describe *why* the change is needed, not just what changed — the diff
  already shows the "what".
