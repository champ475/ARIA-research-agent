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

- `agents/graph_orchestrator.py` — the orchestration entry point (`GraphOrchestrator`),
  a drop-in `process_stream()`/`classify()`/`generate_suggestions()`/`load_history()`
  adapter over the LangGraph engine in `graph/`. Also resolves each user's
  LLM provider (bring-your-own key vs. the free-trial quota) before every turn.
- `agents/llm_client.py` — `LLMClient`: trial path (Groq→Gemini fallback) with
  no `provider_config`, or a single BYO provider (no fallback) with one.
- `agents/providers/openai_compatible.py` — the one BYO adapter, works with
  any OpenAI-compatible `base_url` (OpenAI, Groq, OpenRouter, Together,
  Fireworks, Ollama, ...).
- `tools/data/` — external data fetchers (yfinance, NSE bhavcopy, news,
  AMFI NAV) and the Parquet cache.
- `tools/analysis/` — pure computation: technicals, fundamentals, risk,
  backtesting, sentiment.
- `graph/` — the LangGraph research engine (see
  `docs/RFC_AGENTIC_RESEARCH_GRAPH.md`): `graph/tool_catalogue.py` wraps
  `tools/` for Planner's tool selection, `graph/nodes/` holds each pipeline
  stage (QueryAnalyzer, Planner, ResearchExecutor, EvidenceSummarizer,
  Reflection, Verification, ConclusionBuilder, CommunicationLayer).
- `utils/` — DB access, auth, ticker-alias resolution, IST time helpers,
  `crypto.py` (encrypts users' own LLM keys before storage).
- `config.py` — single source of truth for token budgets, sector universe,
  and technical-analysis defaults. Change limits here, not inline in nodes.

## Adding a new domain or tool

1. New data fetcher → `tools/data/`; new computation → `tools/analysis/`.
   Keep tools pure (input → output), no LLM calls inside them.
2. New tool → register a `ToolSpec` in `graph/tool_catalogue.py` (name,
   evidence_type, depends_on, cost, fn, description) — Planner selects
   tools by reading each `description` against the objective, so no
   separate routing step is needed.
3. If the new capability needs its own response-formatting logic (like
   report/screener's file-output branches), add it to
   `graph/nodes/communication_layer.py`.
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
