## What & why

<!-- The problem this solves, not just what changed — the diff already shows the "what". -->

## Testing

- [ ] `python -m pytest tests/ -q` passes (integration tests against a throwaway Postgres, never `.env`'s)
- [ ] `ruff check . && ruff format --check . && mypy` pass
- [ ] `make eval` is green (re-recorded fixtures if a prompt changed)
- [ ] `E2E=1 python -m pytest tests/e2e` passes, if the UI or a chat flow changed

## Checklist

- [ ] Scoped to one change (unrelated cleanups are a separate PR)
- [ ] Token budgets respected (`config.py`'s `TOKENS_*`, not hardcoded)
- [ ] Schema changes are a new Alembic revision; destructive ones say `CONTRACT:` (docs/RELEASING.md)
- [ ] New `utils/db.py` functions taking `user_id` are covered in `tests/integration/test_tenant_isolation.py`
- [ ] New outside data sources are in `docs/DATA_SOURCES.md`
- [ ] Added an `ARCHITECTURE.md` entry if this changes a system boundary, persistence model, provider choice or trust contract
- [ ] **Docs updated**: CLAUDE.md, README and docs/ match what the code now does (features, commands, contracts, runbook)
