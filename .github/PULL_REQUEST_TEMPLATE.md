## What & why

<!-- The problem this solves, not just what changed — the diff already shows the "what". -->

## Testing

- [ ] `python -m pytest tests/ -q` passes
- [ ] Manually exercised the affected chat flow in the UI

## Checklist

- [ ] Scoped to one change (unrelated cleanups are a separate PR)
- [ ] Token budgets respected (`config.py`'s `TOKENS_*`, not hardcoded)
- [ ] Added an `ARCHITECTURE.md` entry if this changes a system boundary, persistence model, or provider choice
