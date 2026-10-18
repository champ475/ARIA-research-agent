# Deploying ARIA

For running an instance for yourself or other people. For a local try-out, the [README quick start](../README.md#quick-start) is enough.

## What runs

| Process | Command | Job |
|---|---|---|
| App | `python app.py` | Web UI, login, API, health endpoints. |
| Worker | `python worker.py` | Background jobs: alert checks, Telegram, nightly data refreshes, clean-up. |
| Postgres | any 14+ | All state. The app never writes to local disk. |

In dev (`APP_ENV=dev`, the default) the app runs the jobs itself. In any other environment, run the worker next to the app. Each job holds a Postgres advisory lock, so extra workers or app replicas never double-run anything.

If `DATABASE_URL` points at a connection pooler (Neon's `-pooler` host), also set `JOBS_DATABASE_URL` to the direct host: advisory locks need a real session.

The schema is managed by migrations. Run `python scripts/migrate.py` after every update. The Docker image does this on start.

Health endpoints: `/healthz` (process is up) and `/readyz` (database, schema and an LLM provider are ready).

## Docker Compose

The reference deployment: app, worker and Postgres.

```bash
cp .env.example .env     # set SECRET_KEY and an LLM key at minimum
docker compose up --build
```

Open http://localhost:7860. Data persists in the `aria_pg_data` volume.

Optional profiles:

```bash
docker compose --profile staging up --build         # separate staging stack on :7861
docker compose --profile observability up           # Prometheus :9090 and Grafana :3000
```

## Production checklist

With `APP_ENV=prod`, ARIA refuses to start unless these are in place:

- [ ] `SECRET_KEY` of 32+ random characters, stored in a password manager.
- [ ] `APP_BASE_URL` starting with `https://`, with the app served behind HTTPS.
- [ ] Real email: `MAIL_BACKEND=smtp` and the `SMTP_*` settings.
- [ ] At least one LLM key (`GROQ_API_KEY_*` or `GEMINI_API_KEY`).
- [ ] `python worker.py` running.
- [ ] `METRICS_TOKEN` set, if you scrape `/metrics`.
- [ ] Backups: `scripts/backup_db.py`, with dumps copied off the machine.
- [ ] [LEGAL.md](../LEGAL.md) reviewed and adjusted to your jurisdiction and setup.

All settings, with defaults, are in `.env.example`. Per-environment differences: [ENVIRONMENTS.md](ENVIRONMENTS.md).

## Hugging Face Spaces

A free, no-card option. Nothing in the app depends on local disk, so a restarting Space loses nothing.

1. Create a Space at [huggingface.co/new-space](https://huggingface.co/new-space) and pick **Docker** as the SDK.
2. Push this repo to the Space's git remote, or sync it from your GitHub fork. The `Dockerfile` works as it is.
3. Under *Settings → Variables and secrets*, add `SECRET_KEY` and `DATABASE_URL` (for example a free Neon database), plus any LLM and Telegram keys.
4. The Space builds and starts on its own.

Free Spaces sleep when idle and cold-start on the next visit. For alerts that fire while the Space sleeps, use the GitHub Actions checker in [ALERTS.md](ALERTS.md).

## Admin access

The **Admin** tab shows only for operators. It lets you look up users, see turns and alerts, disable accounts and flip feature flags. Make someone an operator:

```bash
python scripts/make_admin.py you@example.com
```

## Operating it

| Doc | Covers |
|---|---|
| [RELEASING.md](RELEASING.md) | Tagged releases, image promotion, rollback, migration rules |
| [RUNBOOK.md](RUNBOOK.md) | What to do for each ops alert; backups and restore |
| [SLO.md](SLO.md) | Service-level objectives and the weekly report |
| [CAPACITY.md](CAPACITY.md) | Cost model and capacity plan |
| [PRIVACY.md](PRIVACY.md) | What data is kept, for how long |
| [SECURITY.md](SECURITY.md) | Reporting vulnerabilities, scope |
