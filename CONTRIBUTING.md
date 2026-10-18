# Contributing to ARIA

Thanks for helping. Bug reports, ideas, docs fixes and pull requests are all welcome. By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to help

- **Report a bug or suggest a feature** with an [issue](https://github.com/champ475/ARIA-research-agent/issues/new/choose). For a wrong answer, include the question, what ARIA said, and the `Error ID` if it showed one.
- **Report a security problem** privately. See [docs/SECURITY.md](docs/SECURITY.md).
- **Send a pull request.** For anything bigger than a small fix, open an issue first so we can agree on the approach.

## Dev setup

You need Python 3.11+ and Docker (the integration tests start a throwaway Postgres in it).

```bash
git clone https://github.com/<your-fork>/ARIA-research-agent.git
cd ARIA-research-agent
python -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                # set DATABASE_URL, SECRET_KEY, an LLM key
python scripts/migrate.py                           # use a dev database, never one with real data
python app.py                                       # http://localhost:7860
python worker.py                                    # background jobs (dev runs them in-app by default)
```

No keys? Run fully offline with a fake LLM and recorded market data:

```bash
ARIA_FAKE_LLM=1 ARIA_FAKE_DATA=1 ARIA_NO_DOTENV=1 python app.py
```

## Checks

CI runs all of these. Run them before pushing:

```bash
python -m pytest tests/ -q               # unit + integration (integration uses TEST_DATABASE_URL, else Docker, else skips)
ruff check . && ruff format --check .    # lint and format
mypy                                     # types
lint-imports                             # layering rules
make eval                                # golden-set answer-quality eval (replay, no network)
```

`pre-commit install` runs the lint, format and type checks on every commit. Browser tests are opt-in: `python -m playwright install chromium`, then `E2E=1 python -m pytest tests/e2e`.

Never point tests at a database with real data.

## Project layout

| Path | What it is |
|---|---|
| `app.py`, `server.py` | Entry points: FastAPI app with the Gradio UI mounted at `/` |
| `worker.py` | Background jobs (alerts, refreshes, clean-up) |
| `ui/` | One module per tab. Renders and wires events only |
| `services/` | What the tabs need: the only layer the UI calls for data. No Gradio inside |
| `agents/` | `GraphOrchestrator` and the LLM client (provider routing, key rotation, metering) |
| `graph/` | The LangGraph research engine: analyse, plan, research, verify, answer |
| `tools/` | Data fetchers (`tools/data/`), analysis (`tools/analysis/`), screener, portfolio, charts |
| `web/` | Server-rendered auth pages, downloads, ops endpoints |
| `utils/` | Database, auth, HTTP and resilience helpers, metrics, tracing |
| `prompts/` | Versioned prompt files |
| `migrations/` | Alembic revisions: the only place the schema changes |
| `evals/` | Golden-set answer-quality evaluation |
| `tests/` | Unit, integration, edge-case and e2e tests |
| `docs/` | User and operator docs |

## Conventions that matter

These are enforced by tests or CI, so a PR that breaks them will fail:

- **Layering.** `ui/` never imports `utils.db` or `tools`. Add a function to `services/` and call that.
- **Schema.** Change the database only with a new Alembic revision in `migrations/versions/`. Never run DDL in app code.
- **Config.** Add settings to `config.Settings` (typed, validated at boot). No `os.getenv` in app code.
- **Logging.** `logging.getLogger(__name__)`. No `print()` in app code.
- **External calls.** HTTP goes through `utils/http.py`; libraries without timeouts go through `utils/resilience.py::guarded_call`.
- **Prompts.** Prompt text lives in `prompts/<name>.vN.md`, loaded with `prompts.get(name)`. Changing a prompt means re-recording its eval fixtures.
- **Errors.** Raise the types in `utils/errors.py`. Never put `str(e)` in user-facing text.
- **Advice boundary.** ARIA is education-only. Never prompt for personal picks. Every answer goes through `graph/guardrails.py::finalize`.
- **Tenant isolation.** A new `utils/db.py` function taking `user_id` needs a case in `tests/integration/test_tenant_isolation.py`.
- **Tickers.** Never trust an LLM-produced ticker. Resolve against the instrument master (`graph/entity_resolution.py`).
- **Timestamps.** Use the IST helpers in `utils/helpers.py`.

## Adding a tool

1. A data fetcher goes in `tools/data/`, a computation in `tools/analysis/`. Keep tools pure: no LLM calls inside.
2. Register a `ToolSpec` in `graph/tool_catalogue.py`. The planner picks tools by reading their descriptions, so a clear description is the routing.
3. Mark it `cacheable` only if its cache key covers every input that changes its output. Tools that write data are never cacheable and run only through `graph/write_policy.py`.
4. Use the token budgets in `config.py`; don't hard-code `max_tokens`.
5. A new outside data source needs a row in `docs/DATA_SOURCES.md`.
6. Add tests next to the existing ones in `tests/`.

## Architecture decisions

Significant decisions (system boundaries, persistence, provider choice, trust contracts) are logged in [ARCHITECTURE.md](ARCHITECTURE.md), newest first. If your change reverses one, add a new entry. Don't edit the old one.

## Pull requests

- One change per PR. Unrelated clean-ups go in their own PR.
- Add or update tests, and update the docs your change touches.
- Explain *why* in the description. The diff already shows *what*.
- Add a line to [CHANGELOG.md](CHANGELOG.md) under "Unreleased" for user-visible changes.
- Keep CI green.

The PR template has the full checklist.
