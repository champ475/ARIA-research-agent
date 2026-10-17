# Environments (OPS-01)

ARIA runs in four environments, set with `APP_ENV`. Each one has its own database, secrets, Telegram bot and LLM keys, so testing a change never touches real users' data or the real bot.

| | dev | test | staging | prod |
|---|---|---|---|---|
| Purpose | your machine | the test suite / CI | production-like rehearsal | real users |
| Database | local Postgres or a personal Neon branch | throwaway (`TEST_DATABASE_URL`, Docker) | own Neon branch, or compose `db-staging` | production Neon project |
| Data | `scripts/seed_staging.py` | fixtures | seeded / PII-scrubbed copy | real |
| Env file | `.env` (from `.env.example`) | none (CI env) | `.env.staging` (from `.env.staging.example`) | secret store only (`.env.prod.example` lists the keys) |
| Telegram bot | none, or your own test bot | none | a second bot (`aria_staging_bot`) | the real bot |
| Email | `MAIL_BACKEND=console` (links in the log) | console | SMTP to a test inbox | SMTP |
| Background jobs | in the app process | — | `worker-staging` | `worker.py` |
| Cookies / verification | not Secure, verification optional | — | Secure, verification required | Secure, verification required |

## Guard rails

- **No production database outside prod.** Set `PROD_DATABASE_HOSTS` to production's database hostnames in every dev and staging env file. Any process (app, worker, scripts) with another `APP_ENV` then refuses to start if `DATABASE_URL` or `JOBS_DATABASE_URL` points there (`config.prod_db_problem`, a boot check).
- **Tests never use the app's database.** The integration fixture refuses to run when `TEST_DATABASE_URL` equals `DATABASE_URL`, and unit tests can't open a database connection at all.
- **Seeding refuses production.** `scripts/seed_staging.py` exits when `APP_ENV=prod` or when the host is a production host.
- **Production credentials live only in production's secret store.** Staging has its own `SECRET_KEY`, bot token, LLM keys and SMTP account. A leaked staging secret then never unlocks production.
- **Prod boot checks.** Production refuses to start without a ≥32-character `SECRET_KEY`, SMTP mail, an `https://` `APP_BASE_URL` and at least one LLM provider (`config.boot_problems`).

## Running staging locally

```bash
cp .env.staging.example .env.staging     # fill in staging-only secrets
docker compose --profile staging up --build
docker compose --profile staging exec app-staging python scripts/seed_staging.py --users 10
# http://localhost:7861 — log in as demo1@staging.example.test / staging-demo-pass
```

## A Neon branch per environment

1. In the Neon console, create branches named `staging` and `dev-<name>` from `main` (production). A branch is a copy-on-write snapshot, so it is fast and free on the free tier.
2. Before anyone uses a branch, scrub the PII on it: overwrite emails with `user<id>@staging.example.test`, clear password hashes, BYO keys, Telegram chat ids and chat history.
3. Put each branch's connection string in that environment's env file. Add production's host to `PROD_DATABASE_HOSTS` in all of them.

Hosting is out of scope for this project (OPS-03). The release pipeline (TEST-07) builds the image and runs the smoke tests against the compose stack. Deployment is a hook to fill in when a host is chosen.
