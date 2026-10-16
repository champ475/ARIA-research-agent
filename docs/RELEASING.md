# Releasing ARIA (PRD TEST-07)

One tag gives one image. That image is tested, then promoted. A rollback points `:stable` back at an image that already passed.

## Cutting a release

1. `main` is green: CI covers lint, types, tests, e2e, eval, image scan and security.
2. Add a changelog entry, then tag `main`: `git tag -a v2.1.0 -m "v2.1.0" && git push origin v2.1.0`. Only the owner pushes tags.
3. `.github/workflows/release.yml` runs three jobs:
   - **build**: one image, pushed to `ghcr.io/champ475/aria-research-agent:<version>` and `:sha-<commit>`. It carries an SBOM and max-mode provenance, plus a GitHub build-provenance attestation (`gh attestation verify oci://… --owner champ475`).
   - **verify**: brings up `compose.test.yml` *with the pushed image, by digest*. The stack is offline: fake LLM, recorded data, its own Postgres. It runs the Playwright smoke journeys (signup, equity, screener, alerts) and the golden-set eval. If anything fails, nothing is promoted, and the traces and compose logs are uploaded.
   - **promote**: retags `:stable` to the verified digest. It runs in the `release` environment, so the owner can require an approval there. The deploy step is a no-op hook because ARIA isn't hosted (OPS-03).

## Migrations: expand / contract

The database never rolls back, so every release must run on the schema of the release after it.

- **Expand** (any release): add tables, nullable columns, columns with defaults, and indexes (`CONCURRENTLY` on big tables). Old code ignores them.
- **Contract** (a later release, once no shipped image reads the old shape): drop or rename columns and tables, change types, add `NOT NULL` without a default. The revision's docstring must say `CONTRACT:` and name the release that stopped using the old shape. `tests/test_migration_policy.py` enforces this for revisions after 0021.
- A rename is expand, then a dual write or backfill, then contract, across at least two releases.
- `alembic downgrade` is for local development only.

## Rolling back

```bash
scripts/rollback.sh v2.0.0                # :stable -> v2.0.0 on GHCR, by digest
IMAGE=aria scripts/rollback.sh v2.0.0     # local images (the drill)
```

Then restart the app and worker on `:stable`. Expand/contract means the older image runs on the current schema. Record the rollback in `docs/drills/`.

## Drill

`docs/drills/2026-10-17-release-rollback.md` records two local releases and a rollback. It uses local image tags; no registry or tag push is involved.
