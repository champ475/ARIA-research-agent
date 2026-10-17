# Release and rollback drill: 2026-10-17 (PRD TEST-07)

**What:** two releases and one rollback, following the steps of `.github/workflows/release.yml`, run locally.
- The images are local (`aria:vX.Y.Z`). The verify step is `compose.test.yml` on the image: fake LLM, recorded data, its own Postgres.
- Smoke is the release subset of the Playwright journeys: signup, equity, screener and alert (4 tests).
- No registry, and no git tag pushed (owner-only).
**Who:** Claude Code session (phase 2), on the owner's Windows dev machine (Docker Desktop 29.8).

## Log

```
[00:23 IST] release 1 — build: aria:v2.0.0 (69a8210f6a10)
[00:23 IST] release 1 — verify: compose.test.yml up on the image (exit 0); smoke: 4 passed, 10 deselected in 18.08s 
[00:23 IST] release 1 — promote: aria:stable -> v2.0.0
[00:23 IST] release 2 — build: aria:v2.0.1 (544073af796f)
[00:24 IST] release 2 — verify on the same database (exit 0); smoke: 4 passed, 10 deselected in 15.57s 
[00:24 IST] release 2 — promote: aria:stable -> v2.0.1
[00:24 IST] rollback — rolled back: aria:stable -> v2.0.0 (was sha256:544073af796fb4ec9ffee992afce21a7a4f8f0d5f72
[00:24 IST] rollback — check: aria:stable has the v2.0.0 image id
[00:24 IST] rollback — restart on :stable against the same database (exit 0); smoke: 4 passed, 10 deselected in 15.26s 
[00:24 IST] guard — unknown version: no such local image: aria:v9.9.9 (exit 1)
[00:24 IST] guard — bad argument: usage: scripts/rollback.sh vX.Y.Z   (the release to go back to) (exit 2)
[00:24 IST] teardown
```

## Result

- Pass. Each release was verified before promotion.
- The rollback moved `:stable` back to the earlier image by id, with no rebuild.
- The rolled-back image ran on the database that the newer release had migrated (expand/contract), and the smoke subset passed.
- `rollback.sh` refused an unknown version and a malformed one.

## Found by the drill (fixed in the same change)

1. **A stray "•••" bubble after an answer.** Gradio drew its own pending bubble on the chatbot. The trigger was the login-time chat reset (and four other events that write to the chatbot) running with default progress. On a slow cold start the bubble outlived the answer. It reproduced on every cold container start, and it is the intermittent timeout seen in TEST-04's flake runs. Those events now use `show_progress="hidden"`. After the fix: 3/3 cold starts green, and the full e2e suite (19 tests) passed against the container.
2. **The screener CSV's column order depended on the evidence cache.** A repeated query is served from the cache, whose JSONB reorders keys, so the CSV came out `de,pb,pe,…` instead of `symbol,name,…`. `utils/screener_files.COLUMNS` now fixes the order.

## Not covered

- The GHCR push, attestation and the `release` environment approval. These need a real tag, which only the owner pushes. Run the first real release with `workflow_dispatch` on an existing tag, or by tagging `v2.1.0`.
- A drill on Docker Desktop also met intermittent host port-forward stalls. Those requests never reached the container, and the native e2e runs never show them.
