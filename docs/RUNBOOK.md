# ARIA runbook (OBS-04)

Every page from the ops watchdog (`utils/watchdog.py`, the worker's `ops_watchdog` job, every 5 minutes) and every Prometheus rule (`ops/prometheus/alerts.yml`) links to a section of this page. Each section covers:
- what the alert means;
- what to check first;
- how to fix it.

Pages go to `OPERATOR_TELEGRAM_CHAT_ID` and/or `OPERATOR_EMAIL`. A condition that stays true gets a reminder every `ALERT_REMINDER_MINUTES` (60 by default), and a recovery sends one "resolved" message. `SELECT * FROM ops_alerts WHERE resolved_at IS NULL` shows what is open.

Useful everywhere:
- `python scripts/find_turn.py <error id>` shows one turn's record and trace.
- `turn_stats_hourly` and `slo_daily` (SQL views) show the shape of the last hours and days.
- `GET /readyz`, `GET /health/data` and `GET /metrics` show the live state of one instance.
- `SELECT * FROM job_heartbeats` shows every background job's last run, owner and error.

---

## error-rate

**Means:** more than 5% of chat turns ended in `error` or `llm_unavailable` over 10 minutes (at least 10 turns). Users are getting "Something went wrong (ref …)".

**First checks:**
1. `SELECT status, intent, count(*) FROM turns WHERE created_at > now() - interval '30 min' GROUP BY 1, 2;` Is it one intent or all of them?
2. Take two error IDs and run `python scripts/find_turn.py <id>`. Is it the same exception?
3. Open `/readyz`. Is the database or the schema at fault?

**Fix:**
- A deploy is to blame: roll back (`scripts/rollback.sh`, TEST-07).
- An LLM provider is down: see [llm-exhausted](#llm-exhausted).
- The database is down: see [db-connections](#db-connections). Neon status: https://neonstatus.com.

## latency

**Means:** equity answers' p95 time to first output is above 2 × the SLO (30 s) over 15 minutes.

**First checks:**
1. The `node_ms` of slow turns (`find_turn.py`): which node is slow? The planner, tools or synthesis?
2. `/health/data`: is a data source half-open and timing out?
3. The provider fallback rate on the dashboard: are calls falling through to slower models?

**Fix:**
- A slow data source: its breaker opens on its own after repeated timeouts. Lower `TOOL_TIMEOUT_SECONDS` if needed.
- A slow model: reorder `ARIA_MODELS_SYNTHESIS`.
- Load: raise `CHAT_CONCURRENCY` only if CPU and DB have headroom.

## heartbeat-stale

**Means:** during market hours, no alert check has succeeded for 15 minutes. Users' price alerts are not being evaluated.

**First checks:**
1. `SELECT * FROM job_heartbeats WHERE job = 'alert_check';` Look at `owner`, `last_status` and `last_error`.
2. Is any worker running (`docker compose ps worker`, or the host's process list)? Is the GitHub Actions "Alert Check" workflow enabled? GitHub disables cron after 60 days without repo activity.
3. If `owner` is set but `last_started_at` is old, a process may hold the advisory lock while hung: `SELECT pid, application_name, state FROM pg_stat_activity WHERE application_name LIKE 'aria-job:%';`

**Fix:**
- Start or restart the worker. A hung lock holder: restart that process, or `SELECT pg_terminate_backend(<pid>)` (the lock drops with the session).
- Re-enable the Actions workflow.

## job-failing

**Means:** a background job (`housekeeping`, `fundamentals_snapshot`, `mf_refresh`, …) failed twice in a row.

**First checks:**
1. `last_error` in `job_heartbeats` holds the exception class. Read the worker's logs around `last_finished_at`.
2. Network jobs (snapshot, MF refresh) usually mean an upstream outage. Check `/health/data`.

**Fix:**
- Upstream outage: wait; the job retries on its next slot. Run it once by hand with `python scripts/refresh_fundamentals.py` or `python scripts/refresh_mf.py`.
- A code error: fix it, deploy, run by hand.

## canary-failing

**Means:** the data canary (TEST-05) failed twice in a row. A source (yfinance, NSE, AMFI, news) is down, is blocking us, or changed its response shape.

**First checks:**
1. `python scripts/data_canary.py` prints each source's result and any schema violation.
2. `/health/data` shows each breaker's state and last success.

**Fix:**
- Blocked: NSE blocks cloud IPs and cookie-less clients. Answers already say "NSE data is temporarily unavailable", which is expected degradation.
- Schema change: update the fetcher and its contract in `tools/data/contracts.py`, then re-record the fixture.
- Long outage: consider the licensed source decision (ARCHITECTURE.md, DATA-02).

## llm-exhausted

**Means:** at least 3 turns in 15 minutes found every LLM provider unavailable.

**First checks:**
1. Look at the startup model probe log (`[model_health]`) and `providers` / `models` on recent turns.
2. Groq and Gemini consoles: is quota used up? Is a key revoked?
3. Was a model retired? Check the provider deprecation pages; the LLM-08 log says `model_gone`.

**Fix:**
- Rotate in fresh keys (`GROQ_API_KEY_n`, `GEMINI_API_KEY`) and restart.
- Repoint `ARIA_MODELS_*` at live models.
- Short-term: set `LOCAL_LLM_BASE_URL` so a local model is the last fallback.

## db-connections

**Means:** more than 80% of Postgres connections are in use (the watchdog uses `pg_stat_activity`; Prometheus uses the app pool gauge).

**First checks:**
1. `SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY 1, 2;` Which client holds them?
2. Look for long-running queries: `state = 'active' AND query_start < now() - interval '1 min'`.

**Fix:**
- Point `DATABASE_URL` at Neon's `-pooler` host.
- Lower `DB_POOL_MAX` per replica.
- Kill a runaway query with `pg_cancel_backend`.

**Statement timeout on Neon.** Neon's `-pooler` host (PgBouncer) refuses `statement_timeout` as a connect option, so ARIA doesn't send it there (`utils/db.py::connection_kwargs`). Set it once per role instead, in the Neon SQL editor:
`ALTER ROLE neondb_owner SET statement_timeout = '5s';` Check it with `SHOW statement_timeout;` on a new connection. Migrations and backups are unaffected: Alembic's DDL here runs well under 5 s, and `pg_dump` sets its own timeout to 0.

## db-size

**Means:** the database is above 80% of `DB_SIZE_LIMIT_MB`. On Neon's free tier, writes fail at the limit.

**First checks:**
1. `SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;`
2. The usual growers are `turns`, `chat_history`, `evidence_cache`, `checkpoint_*`, `audit_log` and `downloads`.

**Fix:**
- Check that housekeeping runs, since it purges `downloads`, sessions and tokens.
- Prune old `evidence_cache` rows and old LangGraph checkpoints.
- Archive `turns` older than 90 days.
- Raise the plan.

## fundamentals-stale

**Means:** the screener's `fundamentals_snapshot` is older than `FUNDAMENTALS_MAX_AGE_HOURS` (26 h). Screens still run, but each answer says the data is old.

**First checks:**
1. Look at the `fundamentals_snapshot` job in `job_heartbeats`. Did it run at 19:30 IST, and did it fail?
2. `/health/data`: is yfinance open?

**Fix:**
- `python scripts/refresh_fundamentals.py`. It resumes and skips fresh rows. A full run takes about 8 minutes.

## retention-overdue

**Means:** a table still holds rows older than its retention period (`utils/retention.py`, COMP-06). The nightly `retention` job (02:30 IST) should have removed them.

**First checks:**
1. `python scripts/retention_report.py` lists each class, its limit and its oldest row.
2. The `retention` row in `job_heartbeats`: did it run, did it fail? A failing class is logged as `[retention] <class> failed` and the other classes still run.
3. Is a class far behind? One run purges at most 40 batches (200,000 rows) per class, so a big backlog clears over several nights.

**Fix:**
- Fix the failing class, then run `python -c "from utils.retention import run_retention; print(run_retention())"` from the app environment.
- `audit_log` is special: its trigger refuses any delete of a row younger than 5 years, so it can only be late if the migration 0024 trigger is missing.

---

## Support: the Admin tab

Give an operator access with `python scripts/make_admin.py you@example.com` (add `--revoke` to take it back). They then see a **🛠 Admin** tab. Every handler checks `users.is_admin` on the server, so a non-admin who calls one gets "403"; everything an admin looks up or changes is in `audit_log` (`admin.*` actions) with their id.

The three questions support gets most, each answered in a minute or two:

| Question | Do this |
|---|---|
| "My alert didn't fire." | Find the user, **Their alerts**. Read *Status* (paused?), *Last checked* (blank means the checker never evaluated it; stale means the `alert_check` job is down: see `heartbeat-stale`), *Last fired* and *Delivery* (a "Waiting in your inbox" alert fired but Telegram failed; *Last error* says why). |
| "Why did my quota run out?" | Find the user: *Quota* shows messages used today, or "own key". **Recent turns** lists each turn's LLM calls, tokens and ₹ cost; a few expensive report turns or a retry loop explains it. |
| "I can't log in." | Find the user: *verified* (no: they never confirmed the email), **DISABLED** (an operator locked it; **Enable account** undoes it), *Live sessions*. A turn's *Error ID* goes to `python scripts/find_turn.py <id>` for the trace. |

Other controls: **Disable account** locks a user out and ends their sessions (you can't disable yourself). **Feature flags**: `signups_open` and `chat_enabled` (turn it off for maintenance: chat answers "paused for maintenance"); a change reaches every replica within 15 seconds.

---

## Backups

**Targets:** RPO ≤ 24 h (at most a day of data lost), RTO ≤ 4 h (back serving within four hours). Test them every quarter, and after any schema or backup change, with `scripts/restore_drill.py`. Each drill leaves a dated record in `docs/drills/`.

**Two independent copies:**
1. **Neon point-in-time restore** is the first line. The history window is 6 hours on the free plan (at most 1 GB of changes, not extendable) and longer on paid plans. Check it in the Neon console: Settings → Postgres → History window (older consoles: Settings → Instant restore). The free plan also includes 1 manual snapshot. Take one before risky changes such as migrations.
2. **Nightly logical dumps** run `python scripts/backup_db.py` (cron, or the host's scheduler, at about 02:00 IST). It writes `aria-<timestamp>.dump` and a `.sha256` to `BACKUP_DIR` and keeps 14 dumps. Copy `BACKUP_DIR` off the machine (object storage or another disk), because a backup on the database's host isn't a backup.

**Restore: Neon (minutes):**
1. In the Neon console: Branches → `main` → Restore. Pick a time just before the incident, or restore to a new branch to inspect it first.
2. If you restored to a new branch, point `DATABASE_URL` / `JOBS_DATABASE_URL` at it and restart the app and the worker.
3. Check `/readyz` is 200, then log in and open a chat.

**Restore: from a dump (self-hosted, or Neon is unavailable):**
1. Create an empty Postgres (a new Neon project/branch, or `docker run postgres:17`).
2. Run `pg_restore --no-owner --no-acl -d "$NEW_URL" aria-<timestamp>.dump`. Check the `.sha256` first.
3. Run `python scripts/migrate.py` against it (a no-op if the dump is at head).
4. Point the app and worker at it, restart, and check `/readyz`.
5. Rehearse the whole thing with `python scripts/restore_drill.py --dump <file>`, which uses a throwaway container.

**Secret escrow.** Losing `SECRET_KEY` makes every stored BYO LLM key unreadable. Users would have to re-enter their keys; nothing else breaks. Keep these in a password manager or secret store that two people can open:
- `SECRET_KEY` (and `SECRET_KEY_PREVIOUS` during a rotation);
- the database credentials;
- the Telegram bot token, or @BotFather access to revoke and reissue it;
- the LLM provider keys, the SMTP app password and `METRICS_TOKEN`.

The restore drill checks that stored keys still decrypt with the escrowed `SECRET_KEY`.
