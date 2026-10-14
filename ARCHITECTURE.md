# ARIA v2 — Architecture Decision Log

This file records significant architectural decisions for ARIA: why the system
is shaped the way it is, not just what it does (the code and `CLAUDE.md` cover
that). Newest entries on top.

**When to add an entry**: any decision that changes a system boundary, a data
flow, a persistence model, a provider/library choice, or a trust/reliability
contract with the user — not routine feature additions or bug fixes (those
belong in `CHANGELOG_ARIA.md`). If a future change reverses or supersedes an
entry here, add a new entry rather than editing the old one, and link back to
it, so the log stays a true history.

**Entry format**: Problem → Alternatives considered → Final decision →
Trade-offs → Future improvements.

---

## 2026-10-14 — The answer model reads rendered evidence, not just claim strings (LLM-02)

**Problem.** CommunicationLayer passed only each Conclusion's one-line
claim. Fund searches, movers and deals became counts, news became a headline
count, and filings were cut to 150 words. Asked for "best ELSS funds" or "why
TCS is down", the model had nothing to quote, so it either refused or
invented names. The system prompt forbade inventing them, but nothing
supplied the real ones.

**Alternatives considered.**
- *Make conclusions richer* (one claim per fund or headline). Rejected: it
  blows up Verification and Reflection, which also read conclusions and are
  budgeted to about 6k characters.
- *Dump the raw payloads.* Rejected: unbounded size (a filing can be
  hundreds of kilobytes), and third-party text would arrive unfenced.

**Final decision.**
- **`graph/evidence_render.py`** renders each evidence type in a bounded
  form:
  - tables: the top 15 rows of the relevant columns, as Markdown;
  - news: 8 deduplicated headlines with source and date;
  - filings: the passages around results, segment and commentary
    headings;
  - pre-rendered Markdown: truncated at a row boundary.
- **Budget.** `SYNTHESIS_EVIDENCE_CHAR_BUDGET` (12k characters) is split by
  relevance to the intent and question, with unused share redistributed.
- **Fencing.** Third-party text is wrapped as untrusted, and numbers are
  kept verbatim for LLM-03.
- **Prompt.** CommunicationLayer appends an "Evidence data" section, and its
  system prompt allows quoting it but nothing outside it. Conclusions
  (Reflection/Verification's view) are unchanged.

**Trade-offs.**
- **Bigger prompts.** Up to about 3k extra tokens per synthesis, more
  latency on the local model.
- **Heuristic passage choice.** Filing passages are picked by heading
  keywords, so an unusually formatted filing can fall back to its opening
  text.
- **Quality gain not yet measured.** The PRD's "completeness +1.0 judge
  point" needs the LLM-01 judge, which needs a cloud key.

**Future improvements.**
- Relevance by embedding similarity, using a local `nomic-embed` model.
- Table-aware PDF extraction.
- Budget by tokens rather than characters.

---

## 2026-10-14 — Education-only advice boundary, enforced in code (COMP-01, LLM-09)

**Problem.** Planning named specific funds and percentages for one person,
reports and "should I buy" answers could carry calls or targets, and the only
disclaimer was a line the prompt *asked* the model to add. Nothing stopped
"guaranteed 30% returns", insider-information or pump-scheme requests.
Personalised advice to the public can fall under SEBI's IA/RA regulations
whatever a footer says.

**Alternatives considered.**
- *Regulated posture (RIA/RA partner).* Rejected by the user for now: it
  needs registration or a partner, records, suitability and grievance
  handling.
- *Rely on prompt instructions.* Rejected: they aren't enforceable, and
  small local models ignore them.
- *An LLM classifier on every turn.* Not used: it adds an extra call and
  latency, especially on the local fallback, and deterministic rules are
  testable.

**Final decision.**
- **Posture.** Education-only, written up in `docs/ADVICE_POLICY.md`
  (**DRAFT, pending counsel review**) and linked from LEGAL.md.
- **`graph/advice_policy.py`** holds the fixed footers, refusal texts and
  the reframe instruction.
- **`graph/guardrails.py`:**
  - `check_input` runs before the graph. It refuses insider, manipulation
    and guaranteed-return requests with no research, no LLM call and no
    quota spent. It reframes personal buy/sell calls and tips (the answer
    prompt gets an explicit no-call instruction) and adds a caution for
    unlisted, penny and grey-market requests.
  - Rules separate "asking about" from "wanting to do".
  - `finalize` runs on every answer, streamed and sync, and inside
    generated report files. It removes promise and personal-call sentences
    (except negated warnings) with a note, and appends the footer by answer
    kind in code.
- **Planning** is now a model portfolio for the risk band: categories, with
  named funds only as examples, and no direct-stock picks.
- **Tests.** A red-team set of 46 prompts is 100% correct; footers are
  verified for every planning, report and research answer kind.

**Trade-offs.**
- **Rules miss novel phrasings.** The red-team set must grow with every
  miss found in traces (OBS-02).
- **Streamed answers can flicker.** A removed sentence is visible while
  streaming until the final text replaces it.
- **"Personal" is approximate.** "Should I buy X" is reframed even when the
  user only wants analysis. Analysis is still delivered.
- **Not legal sign-off.** The policy needs counsel review before a public
  launch.

**Future improvements.**
- An optional classifier for borderline phrasings, measured against the
  golden set (LLM-01).
- Buffer the last sentence of a stream before showing it (LLM-03 does the
  same for numbers).
- Versioned consent and terms (COMP-02).

---

## 2026-10-14 — Alerts are a validated AlertSpec before they're saved (VAL-04)

**Problem.** Alert creation took the last number in the sentence ("below
3500 by Friday 2026" → 2026). It accepted impossible values (RSI 150,
negative prices) and saved alerts that would fire on the next check. It
also saved exact duplicates, had no per-user cap, and `pct_change` used
`abs()`, so a "drops 5%" alert fired on a 5% rise. With two alerts on one
stock, cancel/pause/resume could only answer "ambiguous".

**Alternatives considered.**
- *Let the LLM extract every condition.* Rejected: it needs a network call
  per alert, the output is unvalidated, and small local models are worse
  at it.
- *Validate only in the database* (CHECK constraints). Rejected: users
  would get a raw error instead of a reason, and the database can't know
  the live price.

**Final decision.**
- **`graph/alert_spec.py`:**
  - A pydantic `AlertSpec` (14 types; per-type ranges; `direction`
    up/down/either for percent and P&L alerts).
  - A deterministic `parse_alert` that reads the number next to the
    comparator, understands Indian amounts (`utils/indian_numbers.py`), and
    returns a spec or an `AlertRefusal` with a reason. The LLM extractor is
    only a fallback, and its JSON passes the same validation.
  - `describe()` produces the plain-words echo in code.
- **Creation** checks the live price. A condition that is already true asks
  for confirmation ("add 'anyway'") instead of saving.
- **Migration `0005`:**
  - adds `direction`, with existing percent rows set to 'either' so their
    behaviour is unchanged;
  - deactivates pre-existing duplicate active alerts, keeping the oldest
    and deleting nothing;
  - adds a partial unique index on active alerts.
- **`utils/db.py::alert_add`** raises `DuplicateAlert` (naming the existing
  id) and `AlertLimitReached`. The cap of 50 active alerts is checked
  inside the INSERT.
- **Management.** Cancel, pause and resume honour "alert #id", restricted
  to the user's own list. When several alerts match a symbol they are
  listed with their ids.
- **Checker.** `scripts/check_alerts.py` honours direction.

**Trade-offs.**
- **Confirmation is a re-ask.** Confirming an already-true alert means
  sending the request again with "anyway"; the pending spec isn't held
  across turns.
- **The cap can be overshot slightly.** It is a count inside the INSERT,
  not a lock, so it could only be exceeded by concurrent creates from the
  same user, and per-user turns are serialised.
- **No live-price check** for indicator alerts: there's no "already true"
  check for those.

**Future improvements.**
- Hold the pending spec in state so "yes" confirms it.
- Add UX-06's alert management screen.
- Let users edit an alert's threshold in place.

---

## 2026-10-14 — Company names resolve against a vendored NSE/BSE instrument master (VAL-02)

**Problem.**
- **Wrong company.** An unrecognised name silently became the previous turn's
  ticker: "XYZQ" after a TCS question was answered about TCS.
- **Guessed entities.** Entities were guessed from capitalised tokens plus a
  live yfinance lookup, so typos, BSE codes and group names ("HDFC",
  "Tata Motors" after the demerger) behaved inconsistently.
- **Stale symbols.** The curated universe had gone stale: `TVS MOTOR`
  (space), `TATAMOTORS` (demerged), `LTIM` (renamed LTM), `KPIT`, `IPCA`.

**Alternatives considered.**
- *Ask the LLM to output tickers.* Rejected: it invents symbols, and local
  models invent more.
- *Live NSE/BSE lookups per turn.* Rejected: slow, rate-limited, and BSE's
  API sits behind bot protection.
- *yfinance Search.* Rejected: networked, fuzzy in undocumented ways, with
  no ambiguity signal.

**Final decision.**
- **Vendored snapshots.** NSE `EQUITY_L.csv` (2,605 instruments) and BSE
  scrip code → ISIN taken from BSE's public daily bhavcopy (5,033 codes,
  2,410 also on NSE), refreshed by `scripts/refresh_instruments.py`.
- **`tools/data/instruments.py::resolve`** returns a typed Resolution
  (found / ambiguous / not_found). Stages, in order:
  1. exact symbol
  2. renamed symbol, with a note (ZOMATO → ETERNAL, TATAMOTORS → TMPV,
     LTIM → LTM)
  3. BSE code via ISIN
  4. alias
  5. company name: an exact name wins unless it is known-ambiguous; a bare
     group name asks
  6. rapidfuzz: the average of a distinctive-word score and a full-name
     score; ≥90 with a margin resolves, 75–90 asks, otherwise not_found
     with suggestions
- **Mentions** (`graph/entity_resolution.py`):
  - **Found deterministically:** aliases and names, `.NS`/`.BO`, BSE
    codes, tickers typed in capitals, curated symbols in any case, and
    words before "share"/"stock".
  - **Masked first:** fund names and market jargon.
  - **From the LLM:** QueryAnalyzer's call returns the companies *as
    written*, and they are only resolved against the master.
- **History** supplies an entity only when the message names no company.
- **Blocking.** For company intents (equity, report, alert), an ambiguous
  name, or a not-found one when nothing resolved, skips research.
  CommunicationLayer then asks deterministically, with no LLM call.
- **Notes.** Renamed-symbol notes and partially unresolved names are
  appended to answers.

**Trade-offs.**
- **Snapshots go stale until refreshed.** A brand-new listing is
  not_found until the next refresh.
- **NSE only.** BSE-only companies are not_found with an explanation,
  because the data layer is NSE-centric.
- **One honest miss in the 200-name table** (99.5%): a made-up name one
  word from a real one gets a weak "did you mean".
- **Re-ask after clarifying.** After a clarification the user restates the
  company. The original question isn't replayed automatically.

**Future improvements.**
- Refresh the snapshots nightly in CI with a PR for review.
- Carry the pending question across a clarification.
- Add an index and ETF master (DATA-05).

---

## 2026-10-14 — Per-turn records in Postgres, optional redacted LangSmith traces (OBS-02)

**Problem.** When an answer was wrong or slow, nothing showed which node,
prompt, tool or provider caused it. `research_journal` was never persisted,
and an error showed users raw exception text with no way to find the turn
behind it.

**Alternatives considered.**
- *Self-hosted Langfuse.* Its stack (ClickHouse, Redis, S3, Postgres) needs
  about 8 GB of RAM, more than the dev machine has.
- *OpenTelemetry only.* No UI until we run a backend.
- *LangSmith via LangChain auto-tracing (`LANGSMITH_TRACING=true`).* This
  uploads LangGraph's full state, including chat history and financial
  profiles, unredacted. Rejected.
- *LangSmith via our own spans.* Chosen, with the user's agreement.

**Final decision.**
- **`utils/tracing.py` is the only LangSmith-aware module.**
- **Turn record.** Each turn opens a `TurnRecord` (a contextvar, so worker
  threads see it). It accumulates:
  - LLM calls, tokens (provider usage, or estimates flagged as such),
    providers, and prompt versions (content hashes of system prompts, so
    there is nothing to bump);
  - tool calls and cache hits;
  - per-node wall time and data gaps;
  - later, the LLM-03 grounding result.
- **Turns row.** Migration `0004_turns` adds the table, and every turn
  writes one row (ok / partial / gated / quota / llm_unavailable / error /
  cancelled). This happens even on exceptions, and DB failures never break
  a turn.
- **Error IDs.** User-facing errors show the turn id prefix as an Error ID.
  `scripts/find_turn.py` finds the row, then the trace link: 2 clicks.
- **LangSmith** (opt-in: `ARIA_TRACING=langsmith` plus `LANGSMITH_API_KEY`):
  - Spans are explicit RunTrees whose parents we track ourselves. We never
    make a run "current" in LangSmith's context, so LangChain auto-tracing
    stays off; a test asserts this.
  - Inputs and outputs pass through `redact_for_trace` (secrets, emails,
    length caps). Node spans record counts and routing facts, never raw
    state.
  - The startup check warns if `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2`
    are set.

**Trade-offs.**
- **Coarser tree.** Our own spans give a coarser tree than LangChain's
  automatic one: no per-step LangGraph internals.
- **Estimated stream tokens.** Streamed calls estimate tokens (characters
  ÷ 4) because providers report usage only on non-streaming calls here.
- **The trace URL arrives later.** A background thread resolves it after
  the row is written.
- **Not verified live.** LangSmith hasn't been exercised against a real
  account: there is no key yet. The adapter is tested against the
  installed `RunTree` API with a mocked client.
- **Data leaves India.** LangSmith stores traces in the US or EU. Before
  enabling it in production, the processor goes in the COMP-03 register.

**Future improvements.**
- Put turns-table metrics on the OBS-03 dashboard.
- Request usage on streams (`stream_options.include_usage`) where every
  provider supports it.
- Sample full prompts in staging only.

---

## 2026-10-14 — Every external call is bounded; turns have a deadline (REL-02)

**Problem.** yfinance's `.info` / `.financials` and `feedparser.parse(url)` take
no timeout at all. The LLM SDKs used library defaults (the OpenAI SDK waits
600 s), plus their own hidden retries on top of `LLMClient`'s. One stalled
upstream could hold a chat turn for minutes with the input locked, and a
source that was down got hit again on every turn.

**Alternatives considered.**
- *`pybreaker`.* Rejected: it is about 100 lines to write ourselves, and we
  need half-open handling that is thread-safe under concurrent turns
  (REL-01).
- *Kill stalled work.* Not possible for Python threads. Instead we bound how
  long the caller waits, in bounded pools; a stuck worker finishes or dies
  on its own.
- *An asyncio rewrite with real cancellation.* Rejected: far too invasive
  for the gain.

**Final decision.**
- **`utils/resilience.py`:**
  - a thread-safe `CircuitBreaker` (closed → open after N failures within a
    window → half-open with exactly one trial call);
  - `call_with_timeout`, with separate "io" and "tool" pools so nesting
    can't deadlock, and caller contextvars carried over so `turn_id`
    survives in logs;
  - `guarded_call(source, fn, timeout=)`.
- **`utils/http.py::GuardedSession`:** default (3, 10) s timeouts, urllib3
  `Retry` with jitter for idempotent requests, and the breaker. A network
  error, 429 or 5xx counts as a failure.
- **Applied to:** AMFI, NSE and BSE (bhavcopy, filings, PDFs), and Google
  News and RSS (fetched with a timeout, then parsed from bytes).
- **yfinance:** every touch goes through `fetcher._yf` (10 s and the
  "yfinance" breaker). Errors fall into the existing BSE → bhavcopy
  fallbacks.
- **LLM calls:**
  - cloud: 30 s per call, 15 s maximum gap between streamed chunks, SDK
    retries off;
  - local CPU model: 300 s.
- **Turn deadline** (`graph/deadline.py`, `TURN_DEADLINE_SECONDS`, default 90):
  - Stamped on each turn and each gate resume.
  - Past 70% of it, ResearchExecutor skips unstarted tasks, except writes
    and their dependencies, and Reflection and Verification stop looping.
  - `execute_task` caps each tool call at `min(25 s, time left)`.
  - Failed or skipped tasks carry a reason, and the orchestrator appends a
    deterministic "Some data is missing: news for TCS (timed out)" line.

**Trade-offs.**
- **Background threads.** A timed-out call keeps running in the background
  until it finishes on its own. The pools are bounded, so a long outage can
  queue work behind stuck workers until the breaker opens.
- **One shared yfinance breaker.** One flaky symbol can temporarily push
  every symbol onto the bhavcopy fallback.
- **LLM calls aren't cut by the deadline.** Only their own timeouts bound
  them, so a slow synthesis can still overrun the turn budget.
- **Local-LLM deployments** get a 600 s default deadline, because a CPU model
  can't answer in 90 s.

**Future improvements.**
- Report breaker state on the OBS-03 dashboard.
- Give the LLM synthesis step a share of the deadline.
- Add per-symbol yfinance breakers if a shared one proves too coarse.

---

## 2026-10-14 — Schema changes are Alembic migrations run as a release step (ARCH-01)

**Problem.** `utils/db.py::init_db()` ran about 30 `CREATE ... IF NOT EXISTS` /
`ALTER ... ADD COLUMN IF NOT EXISTS` statements on every app boot, and
`scripts/check_alerts.py` ran its own DDL on every cron tick.
- No record of which schema version a database is on.
- No way to roll back.
- No safe path for type changes, constraints or foreign-key changes. Phase 1
  needs these: the VAL-04 unique index, DATA-04 tables, the OBS-02 turns
  table.
- Every replica raced to run DDL at startup.

**Alternatives considered.**
- *Keep idempotent DDL in `init_db`.* Rejected: it can't express reversible
  or data-moving changes and has no version record.
- *yoyo or plain numbered SQL files with a home-made runner.* Rejected:
  Alembic is the standard and adds only a light SQLAlchemy dependency. We
  use it with raw SQL (`op.execute`), not models.
- *`alembic stamp` production at the baseline.* Rejected: it needs a manual,
  error-prone step on the production DB. Instead the baseline is written
  idempotently (`IF NOT EXISTS`), so production simply runs `upgrade head`.

**Final decision.**
- **Revisions.** `migrations/versions/` holds:
  - `0001_baseline`: the exact old `init_db` DDL, plus DATA-08's delivery
    columns and the DATA-01 purge.
  - `0002_fk_cascade_indexes`: every user-owned FK becomes `ON DELETE
    CASCADE`, found by catalog lookup rather than an assumed name. Adds
    indexes on `chat_history(session_id, ts)`, `alerts(user_id)` and
    `reports(user_id, created_at)`.
- **Release step.** `scripts/migrate.py` runs `alembic upgrade head`, then
  creates LangGraph's checkpoint tables (moved out of
  `get_postgres_checkpointer`). The Dockerfile runs it before `app.py`.
- **Startup.** `utils/db.py::ensure_schema()` only reads `alembic_version`
  and the checkpoint table, and refuses to start with "run `python
  scripts/migrate.py`" when the database is behind.
  `RUN_MIGRATIONS_ON_START=true` exists for single-instance hosts.
- **Alert job.** The checker checks the schema and exits 1 if it is behind;
  it never runs DDL.
- **Tests.** Integration tests migrate the test database the same way a
  deploy does. They also prove:
  - a database built from a frozen copy of the old DDL
    (`tests/integration/legacy_schema.sql`) upgrades in place and keeps its
    rows;
  - cascades work;
  - the last revision downgrades and re-applies;
  - a re-run is a no-op.

**Trade-offs.**
- **Deploy order matters.** Code that needs a new revision fails fast
  against an un-migrated database, by design.
- **The alert job depends on the deploy.** The GitHub Actions alert job
  stops working until production has been migrated. Deploying means running
  `scripts/migrate.py` against Neon, and that is the operator's step.
- **The baseline can't be undone safely.** Downgrading it drops everything,
  so it refuses unless `ALEMBIC_ALLOW_DESTRUCTIVE=1`.

**Future improvements.**
- Run the migration as a dedicated release job in the TEST-07 pipeline
  instead of on container start once there are several replicas.
- Add row-level security policies (SEC-13 backstop) as a revision.

---

## 2026-10-14 — Inbox-delivered alerts stay pending until read (supersedes part of "Alert delivery contract")

**Problem.** The first DATA-08 implementation (entry below) wrote the inbox
row and immediately marked the alert triggered: a one-shot alert was
deactivated before anyone had seen it. The PRD's Done-when says an unlinked
user's alert "stays active with pending_delivery and shows in-app".

**Alternatives considered.**
- *Keep it triggered on insert.* Rejected: an unread row is not proof of
  delivery, which is the whole point of DATA-08.
- *Leave it active and re-evaluate it every run.* Rejected: a condition that
  stays true would add a duplicate inbox row every 5 minutes.

**Final decision.**
- **Queuing.** When `scripts/check_alerts.py` queues an alert in the inbox, it
  sets `delivery_status='pending_delivery'` and leaves `active` and
  `triggered_at` untouched.
- **No duplicates.** Each run's SELECT skips alerts that are already pending.
- **Consuming.** `utils/db.py::notifications_mark_read` consumes them. One
  statement (a data-modifying CTE) marks the notifications read and, for the
  same user's pending alerts they point to, does what Telegram delivery does:
  a one-shot alert is deactivated; a recurring one gets `triggered_at=now()`
  and starts its cooldown.
- **Tests.** `tests/integration/test_alert_delivery.py` proves the whole cycle
  on real Postgres, including that another user's mark-read can't consume
  them.

**Trade-offs.**
- **Unread means never consumed.** A user who never opens the inbox keeps a
  pending alert indefinitely. It is never re-sent, but it never expires
  either.
- **Linking later doesn't re-send.** If Telegram is linked while an alert is
  pending, the alert is not re-sent there; it stays in the inbox.

**Future improvements.**
- Expire pending alerts after N days.
- Deliver pending inbox items over Telegram once the user links it.

---

## 2026-10-14 — A local LLM is the last fallback tier, behind trial and BYO keys

**Problem.** When the shared Groq/Gemini keys are rate-limited, revoked or
misconfigured, every chat turn fails. On 2026-10-14 that was the live state:
Groq returned `model_not_found` for both models and the Gemini key was
invalid. The operator already runs a llama.cpp server (Qwen2.5-3B, CPU, about
5 tokens/s) that speaks the OpenAI API.

**Alternatives considered.**
- *Local first, cloud as fallback.* Rejected: a 3B CPU model is much weaker
  and slower than Llama 3.3 70B, so it should only answer when nothing better
  can.
- *Route through the BYO path (`OpenAICompatibleProvider`).* Rejected: that
  adapter enforces the SEC-01 public-https rule, which correctly rejects
  `127.0.0.1`. The local URL comes from the operator's environment, not from
  a user, so it is trusted config and gets its own provider.
- *LiteLLM proxy in front of every provider.* Rejected for now: it is another
  service to run, and it would bypass `_KeyRotator`.

**Final decision.** `agents/providers/local_llm.py::LocalLLMProvider`
subclasses the OpenAI-compatible adapter (shared `_bind`). `LLMClient` adds it
as a third tier (`_chain()` = primary, secondary, local) when
`LOCAL_LLM_BASE_URL` is set. It is off by default.
- **Order.** It is used in both `_call` and `stream`, only after the cloud
  providers fail.
- **Retries.** It is tried at most once per call. A refused connection, a
  timeout or a context overflow will not be fixed by retrying.
- **Limits.** The SDK's own retries are off. Output is capped at
  `LOCAL_LLM_MAX_TOKENS` (default 1024) and each call is bounded by
  `LOCAL_LLM_TIMEOUT_SECONDS` (default 300).
- **Errors.** llama.cpp's `exceed_context_size_error` is classified as
  `too_large`.
- **No cloud keys.** With neither Groq nor Gemini configured, the trial path
  runs on the local model alone.

It also backs **BYO** clients. This partly supersedes the 2026-10 "BYO has no
fallback chain" stance. That rule existed so a user's failure never spends
the operator's cloud quota, and a local model spends no quota. The Settings
"Test connection" button still builds the BYO provider directly, so a bad
user key is still reported there.

**Trade-offs.**
- **Quality.** Answers from the fallback are much weaker: a 3B model with a
  4–8k context.
- **Speed.** One server slot (`-np 1`) serialises the graph's parallel LLM
  calls, so a fallback turn can take minutes.
- **Visibility.** A BYO user with a broken key gets local answers in chat
  instead of an error, unless they press "Test connection".
- **Deployment.** On a host without the server, every exhausted call makes
  one extra fast-failing connection attempt and logs one warning.

**Future improvements.**
- Mark the reply when it came from the fallback ("answered by local model").
- Route Groq's fast-model calls to a separate `local-tiny` endpoint.
- Add a short health-cache TTL so a down server is skipped without trying to
  connect.
- Trim prompts to fit the local context window.

---

## 2026-10-14 — Tenant isolation is proven by a real-Postgres test suite with a coverage meta-test

**Problem.** Every user-scoped query relies on a hand-written `WHERE user_id=%s`;
unit tests mock the database, so nothing proved the clauses exist or work, and
the shared-cache bug (DATA-01) and two unchecked session writes showed it had
already broken.

**Alternatives considered.** Postgres Row-Level Security now (the strongest
backstop, but every connection would need `SET app.user_id` plumbed through the
pool, the checkpointer and the alert script — a larger change than Phase 0
allows); an in-memory SQLite stand-in (different SQL dialect: `ON CONFLICT`,
`ANY(%s)`, JSONB and partial indexes would diverge from production); mock-based
tests of the SQL strings (proves text, not behaviour).

**Final decision.** An integration suite on a real Postgres (CI service
container, or testcontainers locally) where user B acts on user A's ids through
every user-scoped db function and graph tool. A meta-test introspects
`utils.db` for functions taking `user_id` and fails if any lacks an entry in the
suite's coverage map. The fixture refuses to run against the app's own
`DATABASE_URL`.

**Trade-offs.** Coverage is by function, not by every parameter combination;
functions that are deliberately cross-user (`alert_get_active_all`, caches,
auth rate limits) are outside the meta-test. Local runs need Docker.

**Future improvements.** Enable Row-Level Security with a per-connection
`app.user_id` as a database-level backstop; extend the meta-test to the app's
Gradio handlers.

## 2026-10-14 — Alert delivery contract: consumed only after delivery, with an in-app inbox fallback

**Problem.** The alert checker marked an alert triggered as soon as its condition
held, regardless of whether the notification reached anyone. Users without a
linked Telegram chat, or whose send failed, silently lost one-shot alerts, and
recurring alerts went quiet for their cooldown.

**Alternatives considered.** Keep firing the alert every run until Telegram
succeeds (spams once it recovers and never helps unlinked users); require
Telegram linking before an alert can be created (blocks a common first-use
flow); a full outbox/worker with exponential backoff (right at scale, more
infrastructure than one cron script needs today).

**Final decision.** Triggering is tied to delivery in one transaction. Telegram
counts only on a confirmed `ok: true`; transient failures are counted on the
alert and retried next run; permanent failures (not linked, no bot) and
repeated failures go to an in-app `notifications` inbox shown in Settings.
Market data is memoised per run, and the workflow can't overlap itself.

**Trade-offs.** A retried alert is re-evaluated, so if the condition stops
holding before Telegram recovers, nothing is sent (the alert stays active,
which is the honest outcome). The inbox is only visible inside the app, and
there's no email channel. Schema changes are still applied with idempotent
`ALTER TABLE` statements, not migrations.

**Future improvements.** Alembic migrations (ARCH-01); an email channel and an
Alerts tab with the inbox (UX-06); a delivery-failure metric and alert (OBS-03/04).

## 2026-10-14 — Concurrency model: N concurrent chat turns server-wide, one per user

**Problem.** Gradio's default concurrency limit of 1 per event serialised all
users behind whichever turn was running (reports take 30 s+). Raising it
exposes two shared-state hazards: the per-user `GraphOrchestrator` (memory,
pending HITL state) isn't safe to drive from two threads, and psycopg2's
`ThreadedConnectionPool` raises instead of waiting when exhausted.

**Alternatives considered.** Async handlers end to end (the graph, LLM SDKs and
yfinance are synchronous; a big rewrite for this phase); one process per user
or a job queue with workers (right for scale, REL-05/OPS-02 territory);
per-user queuing that waits instead of rejecting (hides a stuck turn and ties
up a worker slot doing nothing).

**Final decision.** Gradio queue with a shared "chat" concurrency pool sized by
`CHAT_CONCURRENCY` (default 8). A per-user lock allows one in-flight turn per
user; a second message is answered immediately with a "still working" reply.
The DB pool is sized by `DB_POOL_MAX` and fronted by a semaphore so callers
wait up to a timeout for a connection.

**Trade-offs.** Threads, not processes: CPU-heavy work (PDF render, indicator
maths) still contends for the GIL, and the in-memory orchestrator cache keeps
the app single-replica. Rate-limit backoff in `LLMClient._call` still uses
`time.sleep`, holding a worker slot while it waits (REL-02). The PRD's
load-test acceptance (p95 TTFT ≤ 1.5× single-user) has a script but has not
been run against a staging deployment yet.

**Future improvements.** Deadline-aware retries (REL-02); move orchestrator
state out of process (OPS-02) so the app can run multiple replicas; run
`scripts/load_test.py` in a staging pipeline (TEST-06).

## 2026-10-14 — BYO LLM egress policy: public https only, checked at save, load and every request

**Problem.** The bring-your-own-key feature lets a user choose the URL the
server calls. Unchecked, that is server-side request forgery: cloud metadata,
the database port or any internal service is reachable, and the raw upstream
error was echoed back to the user, turning it into a read primitive.

**Alternatives considered.** A fixed allowlist of known providers (safest, but
blocks the many OpenAI-compatible hosts users legitimately use); validating
only at save time (defeated by DNS rebinding and by configs saved before the
check); running BYO calls through an egress proxy with network-level deny
rules (best defence in depth, but needs infrastructure the app doesn't have yet).

**Final decision.** `utils/url_safety.py` enforces "https, and every resolved
address is globally routable". It runs when the URL is saved, when the provider
is constructed from a stored config, and on every HTTP request via a custom
httpx transport; redirects aren't followed. Users see only a coarse error class,
never upstream response text. An operator flag (`BYO_ALLOW_PRIVATE_HOSTS`)
relaxes it for self-hosted deployments.

**Trade-offs.** The per-request check resolves the name and httpx resolves it
again to connect, so a resolver flipping between the two lookups could still
win (small window). Each BYO request pays one extra DNS lookup.

**Future improvements.** Pin the connection to the checked IP (custom
connection pool) or route BYO traffic through an egress proxy with
network-level rules; add a DAST check in CI.

## 2026-10-14 — Write-tool trust boundary: deterministic intent + verb gate, enforced in three places

**Problem.** The research graph lets an LLM choose tools, and five tools write
user data. The LLM's inputs include text the user didn't write (news, filing
PDFs, earlier turns), so a prompt injection or a plain misread could create,
cancel, pause or resume alerts or save screens without the user asking.

**Alternatives considered.** A human-in-the-loop confirmation before every
write (safest, but adds a round-trip to the most common alert flow and the
current HITL gate is typed yes/no only — revisit with UX-02 buttons); an LLM
"is this write intended?" judge (itself injectable and non-deterministic);
removing writes from the graph entirely and routing them through dedicated UI
(right long-term for management actions, but a larger UX change).

**Final decision.** `graph/write_policy.py` owns a deterministic rule: a write
tool runs only when the classified intent owns it and the user's own message
contains an explicit action verb for it. Planner hides/drops disallowed write
tools, Reflection can never add one, and `execute_task` re-checks immediately
before the call and records a "blocked" result. Untrusted text is fenced in
`<untrusted>` blocks with a system notice as defence in depth.

**Trade-offs.** Depends on QueryAnalyzer's intent being right; a user whose
phrasing lacks a recognised verb ("TCS alert below 3500") now gets no alert.
A user who *does* type "cancel my alerts" inside an alert-intent message is
trusted — the gate stops third-party text, not the user.

**Future improvements.** ID-based alert management with confirmation buttons
(VAL-04, UX-02); log blocked writes as a metric (OBS-03) to tune the verb list.

## 2026-10-14 — Evidence-cache contract: a tool is cacheable only if its key covers every input

**Problem.** The shared evidence cache keyed every tool on (tool, entity) for all
users. Tools whose output depends on the user (alerts) or the free-text query
(screener, top movers) — and tools with side effects — were served from cache,
so writes were silently skipped and one user's results answered another's.

**Alternatives considered.** Per-user cache partitions (would still skip writes
and lose the cross-user sharing that makes the cache worth having for
market data); dropping the cache for all `market`-sentinel tools (simple, but
throws away legitimate sharing for movers and screens); adding a generic
`args` hash of the whole context (dependency payloads include DataFrames and
large blobs, so the key would be expensive and unstable).

**Final decision.** Each `ToolSpec` declares `side_effect`, `cacheable` and an
optional `cache_key(entity, context)` returning the extra inputs that change
its output. Write tools and user-scoped reads are `cacheable=False`;
query-driven tools key on their parsed input. A key version is part of the hash
so a scheme change orphans old rows, and a catalogue invariant test fails CI if
a write tool is cacheable or a tool reading `_query`/`_user_id` is cached
without keying on it.

**Trade-offs.** The invariant test detects context reads by source inspection,
so a tool that reaches `_query` indirectly through a helper relies on review.
Old orphaned rows stay in the table until a retention job (DATA-10) clears them.

**Future improvements.** Typed task args (instead of context keys) so cache keys
derive from declared inputs automatically; a retention sweep for stale rows.

## 2026-10-14 — Logs are JSON lines with a per-turn correlation id

**Problem.** No logging was configured at all, so library and app warnings
went to Python's last-resort stderr handler as bare text, debug traces used
`print()` (one dumped full LLM prompts), and nothing linked a log line to the
user turn that produced it. Debugging a bad answer meant guessing.

**Alternatives considered.** `structlog` (nicer API, but a new dependency and
every module would need converting to its logger); plain-text logs with a
`%(turn_id)s` format field (not machine-parseable by a hosted sink);
OpenTelemetry logging now (right long-term answer, but OBS-02 tracing is the
place to introduce it).

**Final decision.** Stdlib `logging` with a custom `JsonFormatter`
(`utils/logging_setup.py`), so existing `logging.getLogger(__name__)` calls
work unchanged. Turn fields live in `contextvars`; `run_in_turn()` runs the
whole chat generator in one persistent Context because Gradio resumes each
generator step in a freshly copied context. The user id is logged only as a
keyed HMAC pseudonym, and a redaction pass masks known secret shapes in
messages and tracebacks.

**Trade-offs.** Redaction is pattern-based, so a secret in an unknown shape
can still leak; logs go to stderr only (no hosted sink is wired, which needs
an account decision). The pseudonym changes if SECRET_KEY rotates.

**Future improvements.** Ship logs to a hosted sink (Loki/Better Stack/Axiom),
add OpenTelemetry trace/span ids (OBS-02), and a ruff T20 rule in CI (TEST-01)
to keep `print()` out.

## 2026-08-10 — LLMClient's exhaustion contract changed from a string return to a raised exception

**Problem.** Live E2E testing found `LLMClient.ask()`/`.stream()` returning a
canned "all providers rate-limited" *string* on total failure — indistinguishable,
at the type level, from a genuine LLM response. Reflection/Verification's JSON
parsers treated a failed `json.loads()` on that string as "0 issues found"
(`except: return []`), so a provider outage silently looked like a clean pass
instead of "couldn't check." Compounding it: the retry loop treated a Groq 413
("Request too large" — the prompt itself was oversized, a deterministic
failure) the same as a transient 429, retrying the identical oversized prompt
`MAX_RETRIES+1` times against each provider before falling through to a
configured Gemini key that reported `limit: 0` on every quota metric (never
provisioned, not "exhausted for today") — so the fallback was attempted every
single call despite being structurally incapable of succeeding. Root cause of
the oversized prompts: Reflection/Verification serialize `evidence_summary`
into the prompt with no size cap, and a single evidence entry (e.g.
`fetch_info`'s 100+ yfinance fields) could alone push the prompt past Groq
free tier's 6,000 TPM cap. Confirmed live: a single turn could burn 3×2
doomed attempts per node, repeated once per self-correction loop iteration
(up to `max_loops`), multiple minutes of pure retry latency for zero benefit.

**Alternatives considered.** Keep the string-return contract and just fix the
JSON-parse-failure ambiguity locally in Reflection/Verification (e.g. a
sentinel string) — rejected, since "an LLM response that happens to look like
an error" is exactly the class of bug a string contract can never fully rule
out, and every future caller would have to remember the convention. Cap only
the retry *count* without classifying error types — rejected, since a 413 on
an already-at-budget prompt is deterministic; retrying it (even fewer times)
still wastes calls a single fast failure avoids entirely. Drop Gemini as a
fallback entirely — rejected, it's still useful when it isn't the specific
zero-quota key configured in this environment; the fix instead makes a
zero-quota state self-detecting.

**Final decision.** `agents/llm_client.py`: (1) `_classify_error()` distinguishes
`too_large` (413/context-length wording) from `rate_limited` (429/quota
wording) from `zero_quota` (a "limit: 0" marker) from `other`. (2) `too_large`
never sleeps and is never retried against the same provider again within that
call — if every configured provider has already rejected the exact prompt,
`_call()` stops immediately instead of exhausting `MAX_RETRIES`. (3)
`zero_quota` marks that provider unavailable in a new module-level
`_ProviderHealthCache` (TTL `config.PROVIDER_ZERO_QUOTA_TTL_SECONDS`, 1 hour)
that subsequent calls check before even attempting the network round-trip.
(4) `LLMClient.ask()`/`.stream()` now raise `LLMProvidersExhausted` on total
failure instead of returning/yielding a string; `format_exhausted_message()`
is the free-function formatter for whichever caller needs to show the user
something. (5) `graph/nodes/reflection.py`/`verification.py` catch it locally
and force `loop_count` to `max_loops` (the field's reducer is
`Annotated[int, operator.add]`) so the self-correction loop exits terminally
instead of silently treating exhaustion as "all clear"; every other caller
(`planner.py`, `query_analyzer.py`, `communication_layer.py`) lets it
propagate to one top-level catch in `agents/graph_orchestrator.py::process_stream`.
(6) New `graph/nodes/evidence_summarizer.py::cap_evidence_for_prompt()` bounds
the evidence payload (`config.EVIDENCE_PROMPT_CHAR_BUDGET`, ~1,500 tokens)
before Reflection/Verification embed it in a prompt, addressing the root
cause rather than just its symptom.

**Trade-offs.** Every direct caller of `LLMClient.ask()`/`.stream()` outside
Reflection/Verification now relies on the top-level `process_stream()` catch
rather than handling exhaustion locally — correct for this app's one
orchestration entry point, but a second caller of `LLMClient` outside
`GraphOrchestrator` would need its own catch. The zero-quota TTL is a
heuristic (1 hour): a key that's fixed sooner stays skipped until the TTL
lapses, and the "limit: 0" substring match is best-effort against however the
SDK stringifies its exception, biased toward false negatives (worst case:
back to today's per-call retry) over false positives (skipping a call that
might have worked).

**Future improvements.** Respect Gemini's `retryDelay` field from a genuine
(non-zero-quota) 429 instead of the current fixed exponential backoff — not
implemented here since the zero-quota cache already eliminates the confirmed
wasted-call case, and parsing `retryDelay` means committing to the exact
`google-genai` SDK error shape. A tokenizer-based (rather than 4-chars-per-token
heuristic) budget for `EVIDENCE_PROMPT_CHAR_BUDGET` would size the cap more
precisely per model.

---

## 2026-08-08 — Auth rate limiting moved from in-memory to Postgres (supersedes the "In-memory sliding-window throttle" entry below)

**Problem.** The entry below explicitly reasoned that an in-memory dict was
"correct for the single-instance deployment this app actually ships as."
That framing was written before this restructure made the app genuinely
multi-user/public-facing — the in-memory approach's actual weakness isn't
multi-instance scaling (still not a concern for this deployment shape), it's
that a restart (redeploy, crash, host recycling a container on an
ephemeral-hosting free tier) silently wipes every tracked attempt, handing
a brute-force attempt a free reset exactly when a public instance is most
exposed to one.

**Alternatives considered.** Keep in-memory, accept the reset-on-restart
gap — rejected now that public exposure is the actual deployment target,
not a hypothetical. Redis — rejected, still true that this app has no
other need for it and a single-instance deployment gets nothing from a
dedicated cache service one small table doesn't already provide.

**Final decision.** New `auth_attempts` Postgres table (key, attempted_at),
queried/inserted by `utils/auth.py::_check_rate_limit` via
`utils/db.py::auth_attempt_count`/`auth_attempt_record`/`auth_attempt_clear` —
same 5-attempts/5-minute window and semantics as before (a successful login
clears its own key), just durable across restarts. No new dependency.

**Trade-offs.** One extra indexed-table query per signup/login attempt
instead of an in-memory dict lookup — negligible next to the PBKDF2 hash
+ user-row lookup already happening in the same call. A small
check-then-insert race under concurrent requests from the same email could
let one or two extra attempts through in the worst case — acceptable for a
login throttle, not a security boundary this app relies on for anything
stronger.

**Future improvements.** None identified — this closes the gap the
superseded entry's own reasoning left open once the deployment context
changed.

---

## 2026-08-08 — Signup requires explicit Terms/Privacy consent

**Problem.** Going open-source and public means unauthenticated strangers
can sign up with no relationship to the app author beyond this software.
The footer disclaimer ("Not SEBI-registered...") was the only place any of
this was stated, and there was no record of anyone having seen or agreed
to anything — a real gap once BYO-key (§ earlier entry) means a user's own
prompts/data flow to a third-party LLM provider the operator doesn't
control.

**Alternatives considered.** A passive "by using this app you agree..."
footer note — rejected, doesn't create an actual consent record and is
easy to never read. A separate legal-acceptance page before the auth
gate — rejected as unnecessary friction for what a checkbox next to the
existing signup form already accomplishes.

**Final decision.** New `LEGAL.md` (Terms of Use + Privacy Notice,
written as a template since ARIA is self-hostable — it explicitly frames
whoever runs a given instance as the "operator" responsible for that
instance's actual practices, not a fixed company). Rendered inline in an
accordion on the signup card (`app.py::_build_auth_gate`), gated by a
required checkbox `_do_signup` checks before calling `signup()`. New
`users.accepted_terms_at` timestamp, set via `utils/db.py::accept_terms()`
right after a successful signup — the consent record.

**Trade-offs.** Existing users created before this change have no
`accepted_terms_at` value (column defaults NULL, no backfill) — acceptable,
consent can't be retroactively manufactured; if this matters for a
specific deployment, re-prompt existing users on next login is a follow-up
someone can add.

**Future improvements.** If an instance's terms ever change materially, a
version/timestamp comparison against `accepted_terms_at` could re-prompt
existing users — not needed yet since this is the first version.

---

## 2026-08-08 — Per-user Telegram alert routing, no webhook

**Problem.** Alerts fired to a single hardcoded `TELEGRAM_CHAT_ID` — that
was always the app author's own chat. Every real user needed their own
destination, and `scripts/check_alerts.py` (a standalone GitHub Actions
cron process, not the Gradio app) had no way to know which chat belonged
to which alert's owner.

**Alternatives considered.** (1) One Telegram bot per user — rejected,
forces every user through BotFather themselves, the opposite of a smooth
signup. (2) A webhook endpoint for Telegram to push `/start` updates to —
rejected for now: requires a public HTTPS URL the app may not have (local
dev, or a host without one configured), whereas long-polling works
anywhere outbound HTTPS does. (3) Ask users to paste their numeric chat ID
manually (via @userinfobot) — works but is exactly the kind of manual,
easy-to-fumble step a "just click connect" flow should remove.

**Final decision.** One bot (unchanged `TELEGRAM_BOT_TOKEN`, still a single
shared value — Telegram's API is chat_id-addressed, one bot already serves
unlimited chats) maps to many linked chats via a new
`users.telegram_chat_id` column. Linking: `utils/telegram_link.py` mints a
short-lived code (`telegram_link_codes` table) shown as a
`t.me/<bot>?start=<code>` deep link in Settings; a background long-polling
thread (started once at app startup, `start_polling_thread()`) reads the
resulting `/start <code>` message via Telegram's `getUpdates` API and
stores the chat_id — no public endpoint needed. `scripts/check_alerts.py`
now JOINs `alerts` against `users.telegram_chat_id` in its existing raw-SQL
query (kept raw deliberately — see the script's own docstring on why it
doesn't use `utils/db.py`'s pooled connection) and sends each alert to its
owner's chat only.

**Trade-offs.** Long-polling means the app process must stay running for
linking to work (an alert-check-only deployment with the web app asleep
would delay a NEW connection attempt until it wakes — existing links keep
firing regardless, since `check_alerts.py` never touches the web app at
all). `TELEGRAM_BOT_USERNAME` is a second env var to configure (needed to
build the deep link) — a small one-time addition for the operator.

**Future improvements.** A webhook-based listener would remove the
"app must be awake to link" caveat, worth revisiting once/if this deploys
somewhere with a stable public HTTPS URL.

---

## 2026-08-08 — Reports render on demand; server never persists a file

**Problem.** Reports were dual-written: full text to Postgres (`reports.content`)
and a `.md`/`.pdf` copy to a server-local `reports/` directory
(`utils/report_files.py::save_report_files`), for the download buttons to
serve. Free/cheap hosting (the actual deployment target for this
open-source project) is commonly ephemeral-disk (containers reset on
restart/redeploy) — a report generated one session would silently 404 on
re-download in the next, even though the text was safe in Postgres the
whole time. There was also no way to re-download an older report at all:
nothing in the graph path read `report_list()`/`report_get()`.

**Alternatives considered.** (1) Keep dual-writing but mount a persistent
volume for `reports/` — works, but couples the deployment to a specific
host's disk-persistence guarantee, and does nothing for hosts that don't
offer one (the free tiers this project is meant to run on). (2) Cache
rendered files in Postgres as bytea — avoids disk entirely but stores a
binary blob per report per format, non-trivial storage growth for no
benefit over just re-rendering (rendering is fast, seconds, and rarely
requested).

**Final decision.** `utils/report_files.py::render_report()` is now a pure,
stateless render: Markdown text → temp-dir `.md`/`.pdf`, always, whether
this is a fresh generation or a redownload. `reports.content` (Postgres) is
the only durable copy. New `_handle_report_management` in
`graph/nodes/communication_layer.py` adds "show my reports" (lists via
`report_list()`) and "download report #N" (fetches via the new
`report_get()`, re-renders via the same `render_report()` fresh-generation
already uses) — one render path, two callers, not two renderers. Deleted
`utils/pdf_export.py` (confirmed dead code) and `config.py`'s `REPORTS_DIR`
along with the repo's tracked `reports/` placeholder.

**Trade-offs.** Every re-download pays a render cost again (Markdown→HTML→PDF
via weasyprint) instead of serving a cached file — acceptable, this is a
low-frequency action and the render itself is fast. Rendered temp files are
never explicitly cleaned up (left to the OS/container lifecycle) — fine on
the ephemeral hosts this targets, would very slowly accumulate on a
long-lived persistent-disk deployment.

**Future improvements.** If re-download volume ever justifies it, a short-TTL
cache (render once, reuse for N minutes) would cut redundant renders
without reintroducing a durability dependency on the render output itself.

---

## 2026-08-08 — LangGraph orchestrator becomes the only path; legacy Orchestrator + 6 specialist agents retired

**Problem.** Two orchestration paths (`agents/orchestrator.py`'s keyword-routed
specialist agents, and the LangGraph engine behind `USE_LANGGRAPH_ORCHESTRATOR`,
default off) meant every behavior change had to be made twice or risked
drifting apart. Cutting over wasn't a one-line flag flip: `GraphOrchestrator`
still hard-imported the legacy `route()` for `classify()`, `generate_suggestions()`
was a stub returning `[]`, `load_history()` was a no-op, equity queries lost
token-by-token streaming, and — the actual blocker — `graph/tool_catalogue.py`
never registered an `alert_create` tool, so no user could create a new alert
through the graph path at all.

**Alternatives considered.** (1) Keep both paths indefinitely, flag-gated —
rejected, doubles maintenance forever for a graph that was always meant to
replace the legacy router once it reached parity. (2) Cut over accepting the
listed regressions as known follow-ups — rejected: losing alert creation
outright is a core feature loss, not an acceptable interim state.

**Final decision.** Closed every gap first — `alert_create` registered
(extracts `alert_type`/`threshold`/`condition` from `context["_query"]` the
same way `screener_parse_query` already reads free text, no Task-schema
change needed), `classify()` reimplemented as a self-contained keyword
scorer, `generate_suggestions()` and `load_history()` implemented for real,
equity/chat turns now stream (CommunicationLayer defers its LLM call via
`pending_synthesis_prompt` when `_is_streamable()`, `GraphOrchestrator`
makes the actual `.stream()` call and yields growing text) — then deleted
`agents/orchestrator.py` and the 6 specialist agent classes
(`equity_agent.py`, `report_agent.py`, `mf_agent.py`, `screener_agent.py`,
`alert_agent.py`, `planning_agent.py`, plus the already-vestigial
`base_agent.py`) along with the `USE_LANGGRAPH_ORCHESTRATOR` flag.

Live verification against the real Neon DB and real Groq/Gemini keys (not
just the stub-based test suite) surfaced three severe, pre-existing bugs in
the Postgres checkpointer path that would have made the graph unsafe as the
sole path — none introduced by this cutover, all now fixed:
- A raw `pandas.DataFrame` in `Evidence.payload` (fetch_ohlcv, fetch_nav)
  crashed every checkpoint write (`ormsgpack` can't serialize a DataFrame).
  Fixed at the source (`graph/nodes/execute_task.py`): Evidence.payload is
  now kept in the same checkpoint-safe form already used for the evidence
  cache (dict of records, index name/dtype preserved for exact
  reconstruction), with `from_cacheable()` called at each of the 4
  consumption sites that need the real DataFrame back (`research_executor.py`,
  `communication_layer.py`, `conclusion_builder.py`, `evidence_summarizer.py`).
  Nested `pandas.Timestamp`/`datetime` values inside those records needed
  the same treatment — the checkpointer's deserializer refuses to
  reconstruct them (a security guard against arbitrary method-call
  deserialization), so they're flattened to ISO strings going in.
- `GraphOrchestrator`'s thread IDs were `user-{id}-turn-{n}`, deterministic
  from an **in-memory** turn counter. Any fresh instance for a user who'd
  talked before (app restart, redeploy, new session object) restarted `n`
  at 0 and collided with that user's old, durably-checkpointed thread —
  LangGraph's reducers then merged stale tasks/conclusions into the new
  turn. Confirmed live: a fresh orchestrator asking about RELIANCE got
  contaminated with a prior process's TCS conclusions. Fixed by making
  thread IDs `user-{id}-{uuid4}` — no cross-process coordination needed,
  no collision possible.
- LangGraph's own `Send` object (its internal fan-out primitive) isn't
  msgpack-serializable either, breaking any real multi-task parallel
  dispatch — untested by the existing suite since its multi-task tests all
  use `InMemorySaver` (no serialization at all). Fixed by constructing the
  checkpointer's `JsonPlusSerializer` with `pickle_fallback=True`
  (`graph/checkpointer.py`), so anything neither side anticipated falls
  back to pickle instead of raising.

Separately, the graph had **no cross-turn conversation memory of any kind**
— `query_analyzer.py` only ever read the current message, and minting a
fresh `thread_id` every turn (deliberately, to dodge the reducer-merge
problem above) meant nothing carried over even within one session. Since
this needed to work "like ChatGPT" per product requirement, `GraphOrchestrator`
now owns a `ConversationMemory` instance (same class every deleted
specialist agent's `LLMClient` used), rendered into a new `history` field on
`GraphState` every invoke; `query_analyzer.py` falls back to the last
mentioned entity from history when the current message names none, and
`communication_layer.py` threads history into both synthesis prompts.

**Trade-offs.** `GraphOrchestrator`'s per-instance `ConversationMemory` is
process-local like the deleted agents' was — restarting the app mid-session
still loses live memory until the next `load_history()` call (session
resume from Postgres `chat_history` already handles that path). A known,
logged-but-not-fixed gap remains: `query_analyzer.py`'s `extract_all_entities()`
runs unconditionally with no `validate_ticker()` filter, so free-text
domains (planning intake) can dispatch real yfinance lookups for ordinary
capitalized English words — wasteful, not corrupting (planning bypasses
evidence/conclusions entirely), pre-existing, not introduced here.

**Future improvements.** Filter `query_analyzer.py`'s extracted entities
through `validate_ticker()` before they reach Planner. Consider whether
`pickle_fallback=True`'s broader acceptance of arbitrary types is worth
tightening once the specific types LangGraph needs (`Send` and whatever
else surfaces) are known, rather than left permissive indefinitely.

---

## 2026-08-08 — In-memory sliding-window throttle on signup/login

**Problem.** Going open-source puts `utils/auth.py`'s signup/login directly
on the public internet with no throttle — unlimited password guesses per
account and unlimited account creation for the cost of an email string.

**Alternatives considered.** (1) A dedicated rate-limit service (Redis,
Cloudflare) — correct for a multi-instance deployment but a hard dependency
this project doesn't otherwise have, and a self-hoster running one Gradio
process gets no benefit from it. (2) A Postgres-backed attempt log — keeps
state across restarts but adds a DB round-trip to every auth attempt before
we even know if it's legitimate, working against the point of a throttle.
(3) In-memory sliding window, keyed by `signup:<email>` / `login:<email>`,
checked before the DB call — chosen: zero new dependencies, cheap, and
correct for the single-instance deployment this app actually ships as.

**Final decision.** `_check_rate_limit()` caps each key at 5 attempts per
5-minute window; a successful login clears its own key so normal typo
recovery isn't penalized. Signup has no equivalent clear (an account either
gets created or the email is invalid/taken, so there's nothing to reset).

**Trade-offs.** Resets on process restart, and doesn't share state across
multiple app instances or protect against distributed attempts from many
IPs/emails at once — this is anti-abuse for a self-hosted single instance,
not a substitute for a real WAF/rate-limit layer in front of a
multi-instance or paid deployment.

**Future improvements.** Move to Postgres or Redis if the app ever runs as
more than one instance; add an IP-based key alongside the email-based one.

## 2026-08-08 — Docker Compose bundles Postgres for open-source onboarding

**Problem.** `utils/db.py` is Postgres-only (`SERIAL`, `JSONB`,
`TIMESTAMPTZ`, `ON CONFLICT`) and the documented setup path required a Neon
account before a first-time user could run the app at all — too much
friction for an open-source project anyone should be able to try in
minutes.

**Alternatives considered.** (1) Add a SQLite fallback so `DATABASE_URL`
unset just works locally — rejected: `utils/db.py`'s schema and every query
in it lean on Postgres-specific syntax throughout; making both dialects
work correctly is a real rewrite, not a quick add, and risks subtly
diverging behavior between the SQLite and Postgres code paths for a
project that has never run on SQLite in production. (2) Ship a hosted demo
instance — rejected: recurring cost and an operational surface this project
doesn't need. (3) `docker-compose.yml` bundling a `postgres:16-alpine`
service alongside the app container — chosen: `docker compose up --build`
gets a first-time user a running app with zero Neon signup, using the exact
same Postgres the app already targets, no schema fork.

**Final decision.** `docker-compose.yml` runs `app` + `db`, with `app`'s
`DATABASE_URL` pinned to the compose-internal `db` service regardless of
what's in `.env` (so a stale Neon URL left over from non-Docker use can't
silently get used instead). Manual `pip install` + Neon setup remains the
documented non-Docker path in the README.

**Trade-offs.** Two supported run paths (manual + Docker) to keep in sync
in docs; Docker users still need `GROQ_API_KEY_1`/`GEMINI_API_KEY` from
outside providers — LLM calls aren't self-hostable the way the DB now is.

**Future improvements.** A SQLite fallback remains the real fix for
zero-external-dependency local dev, if `utils/db.py` is ever worth
rewriting dialect-agnostic (e.g. via SQLAlchemy).

## 2026-07-29 — Rolling bhavcopy history as a second-tier OHLCV fallback

**Problem.** A Playwright-driven end-to-end audit caught yfinance rate-limited
for an entire session (`Too Many Requests` on every NSE and BSE call). The
existing degraded path (`tools/data/fetcher.py::get_ohlcv()`) already fell
back to NSE bhavcopy for a last-known price, but only as a single EOD row —
enough for a CMP, not enough for RSI/MACD/SMA/pivots/Fibonacci, which all
need a multi-day window and every one of them rendered `None` across every
equity query in that session (single-ticker and 2-ticker compare alike).
`fetcher.py`'s own docstring already names yfinance as a single point of
failure for the whole app; this was that risk materializing concretely, not
a hypothetical.

**Alternatives considered.** (1) A second live data provider (e.g. an
alternate free NSE/BSE OHLCV API) as a true parallel source — rejected for
now: another provider is another rate limit, another schema to normalize,
and another thing that can silently drift from yfinance's numbers; out of
scope for a same-day fix. (2) Widen the existing single bhavcopy row into a
synthetic multi-day series (e.g. repeat the one known close) purely so
indicator math doesn't crash — rejected: fabricated repeated candles would
make RSI/MACD compute *something*, but that something is meaningless and
actively misleading, worse than the current honest "None." (3) Accumulate
real bhavcopy rows over time — the option taken.

**Final decision.** New `tools/data/bhavcopy_history.py`: every time
`get_ohlcv()`'s existing bhavcopy fallback fires for a symbol, that day's row
is appended to a per-symbol rolling Parquet file (`data/bhavcopy_history/
{symbol}.parquet`), deduped by calendar day, capped at 300 rows. `get_ohlcv()`
now prefers that accumulated window over the old single-row fallback once
it has 2+ real rows on file. This reuses the bhavcopy fetch that was already
happening — no new network calls, no new provider dependency, and it composes
with the existing single-row fallback (which still fires on day one before
any history has accumulated) rather than replacing it.

**Trade-offs.** This cannot backfill history from before it started running —
a fresh deployment still shows blank technicals for roughly the first 14
trading days of a sustained outage (RSI-14's minimum window), only degrading
gracefully after that point. It's also bhavcopy-only data (EOD close, no
intraday), so even once populated it can't support intraday-interval charts,
and every row is dated to session close, not to when the request happened.
`_bhavcopy_row()`'s own once-per-day cache means this only advances the
window at most once per calendar day per symbol, by design.

**Future improvements.** A startup or scheduled backfill job that seeds
recent bhavcopy history for the curated `SECTOR_UNIVERSE` symbols ahead of
time would close the "first 14 days" gap without waiting for organic traffic
to trigger it. Longer term, this rolling store is also the natural place to
add a genuinely separate secondary OHLCV provider (alternative considered
above) behind the same `read_history()` interface, without another change to
`get_ohlcv()`'s call sites.

---

## 2026-07-24 — Thread-id scoped per turn (not per user); multi-intent via secondary_intents; staged retirement deferred

**Problem (thread-id).** `agents/graph_orchestrator.py` used one Postgres-checkpointed
LangGraph thread per user (`f"user-{user_id}"`), persisted across every turn. Every
reducer-backed `GraphState` field (`tasks`, `evidence`, `research_journal`, `loop_count`) is
append-only/merge-only by design (so Reflection can add tasks mid-run without wiping
already-`done` ones) — which means a fresh `graph.invoke()` call under the same thread_id
does NOT start from a clean slate; it merges the new call's initial dict against whatever
was last checkpointed for that thread. A prior turn's leftover `Task`/`Evidence` entries (or
a paused HITL gate) could bleed into the next, differently-intentioned turn.

**Alternatives considered.** (1) The plan's original framing — "explicitly clear the
in-flight fields at the start of every new turn" — turned out not to be implementable as
described: passing `"tasks": []` on invoke doesn't clear anything, because `upsert_by_id`'s
reducer only ever adds entries from the new value, never removes existing ones from the old.
There is no LangGraph-native way to make a reducer-backed channel "shrink." (2) A
conversation-scoped thread_id (`user-{id}-conv-{conversation_id}`) — floated in the original
plan as the alternative to per-turn reset, with a minted conversation id — this still has the
exact same leakage problem WITHIN a conversation (turn 2 of the same conversation still
inherits turn 1's checkpointed Tasks), so it doesn't actually solve the problem, just narrows
its window.

**Final decision.** Mint a brand-new thread_id per top-level (non-resume) turn:
`user-{id}-turn-{n}`, incrementing `n` on every fresh call to `process_stream()`. A resume
call (`_awaiting_confirmation` True, following a HITL interrupt) reuses the SAME thread_id,
since LangGraph's interrupt/resume mechanism requires it. This sidesteps the reducer problem
entirely — there's no prior checkpoint to merge against when the thread_id itself is new, so
there's nothing to leak. `reset()` and the newly-added `load_history()` (see below) both start
a fresh thread too.

**Bug fixed in passing**: `GraphOrchestrator` had no `load_history()` method at all —
`app.py`'s sidebar session-resume flow calls `get_orchestrator(user_id).load_history(hist)`
unconditionally on whatever orchestrator is active, so resuming a past chat session with
`USE_LANGGRAPH_ORCHESTRATOR=true` would have raised `AttributeError`. Added as a no-op (plus a
thread reset) — the graph's per-node LLMClients don't hold persistent multi-turn memory the
way `EquityAgent`'s does, so there's nothing to seed yet.

**Multi-intent-in-one-turn.** Implemented via `QueryAnalyzer`'s new `secondary_intents` field
rather than extending `Planner`, because `Planner` never needed a change: it already
free-selects tools from the *whole* catalogue based on `objective` text, never gated by
`intent` — `intent` is only consumed by `CommunicationLayer`'s domain-specific file-output
branches and by `GraphOrchestrator`'s `tab_used` label. `CommunicationLayer` now checks
membership in `{intent} | secondary_intents` instead of equality against `intent` alone, so a
message like "generate a report on TCS and alert me if it drops 5%" still writes the report
file even though `alert` might be classified as the (arbitrary) primary intent.
`tab_used` becomes a `"+"`-joined string (e.g. `"report+alert"`) — no change to the
`(text, chart, pdf_path, md_path, csv_path, tab_used)` 6-tuple contract, `app.py`'s new
`_tab_label()` helper renders the joined badge. Planning is excluded from this scheme on
purpose — it's checked first and unconditionally short-circuits (see the "Planning domain on
the graph" entry below), so combining planning with another domain in one turn is not
supported.

**Staged retirement — deferred, not attempted.** The plan called for a `LANGGRAPH_DOMAINS`
per-domain rollout flag and incrementally deleting each legacy specialist agent behind it.
Neither was built. The plan itself specifies each retirement step needs a real production
burn-in period before the next domain is retired — that can't happen inside a single
development session, and building the flag now, with no actual retirement to gate, would be
exactly the kind of speculative-future-requirement plumbing this codebase's own conventions
warn against. `USE_LANGGRAPH_ORCHESTRATOR` remains the only rollout flag; all 5 non-equity
legacy agents and `agents/orchestrator.py::route()` are untouched and still the default path.

**Trade-offs.** Per-turn thread_id means Postgres accumulates one checkpoint row per turn
per user indefinitely (no expiry/cleanup implemented) — acceptable for now since checkpoint
rows are small and never queried outside their own turn, but worth a retention policy before
this runs at scale. Multi-intent support doesn't extend to planning combined with another
domain — a real, documented gap, not a silent omission.

**Future improvements.** A Postgres checkpoint-row retention/cleanup job (e.g. delete
checkpoints older than N days) before this ships to real production traffic at scale. Staged
retirement (with or without a per-domain flag) once there's an actual production deployment
to observe burn-in against.

---

## 2026-07-24 — Planning domain on the graph: bypasses Evidence/Conclusion pipeline; 3rd HITL gate deferred

**Problem.** Phase 7.3 needed to bring `planning_agent.py`'s multi-turn financial-planning
intake onto the graph. Unlike report/mf/screener/alert, planning's actual logic is two
sequential LLM calls with Python-only branching between them (does the minimum profile
have all 5 required fields? did the user explicitly ask to build?), reading/writing
`financial_profiles` directly — there is no tool-selection/evidence-gathering step for
Planner or ResearchExecutor to usefully do: nothing in `graph/tool_catalogue.py` models
"extract structured profile fields from free text," and forcing one into existence would
mean a ToolSpec whose `fn` calls the LLM — which breaks CommunicationLayer's "only node
allowed to call the primary model" invariant (RFC Sec6), since `execute_task_node` runs
tool functions outside CommunicationLayer entirely.

**Alternatives considered.** (1) Model intake as ToolSpecs anyway (e.g. `planning_intake`,
`planning_build_strategy`, mirroring `RFC`'s original `Task.args` idea floated in Phase 7.2's
alert_create writeup) — rejected once the LLM-call-inside-a-tool problem above was clear:
it would either violate the CommunicationLayer-only-LLM-caller invariant, or need a second,
parallel "tools that are allowed to call an LLM" concept just for this one domain. (2) A
brand-new pre-CommunicationLayer node pair (`planning_intake_node`, `planning_strategy_node`)
with real conditional graph edges, positioned where a proper HITL gate could sit between them
— the architecturally cleanest option, and the one a future phase should actually build, but
a materially bigger change (new graph.py wiring, new conditional-edge functions, checkpointer
implications for a domain that pauses mid-flow) than this phase's other domains needed.

**Final decision.** For Phase 7.3, planning intent is handled inside
`graph/nodes/communication_layer.py` (`_handle_planning`), checked first, before the
"simple"/"no conclusions" branches — it never touches Planner, ResearchExecutor, evidence,
or Conclusions at all. The actual intake/strategy logic was extracted from
`agents/planning_agent.py` into `utils/planning_logic.py` (mirroring the
`utils/report_files.py`/`utils/screener_files.py` precedent from 7.1/7.2) so the legacy
agent and the graph path run identical logic against the same `financial_profiles` row.
`agents/planning_agent.py` is now a thin wrapper delegating to the shared module.

**The 3rd HITL gate (capital-allocation confirmation, stubbed at `graph/nodes/human_review.py:12-14`)
is explicitly NOT implemented in this phase.** A gate needs a real pause point the
checkpointer can resume from — LangGraph's `interrupt()` is designed to be called from a node
positioned mid-pipeline with a conditional edge on either side of it (exactly how the existing
2 gates work), not from inside a terminal node that also handles 5 other unrelated
responsibilities (report files, screener CSV, chart rendering, the generic conclusion-based
synthesis path). Bolting `interrupt()` into `_handle_planning` today would technically run, but
would conflate "pause for user confirmation on this specific decision" with "this node's whole
execution restarts from the top on resume," which is exactly the trap alert_create and
screener's cross-entity fix each specifically avoided by deferring rather than force-fitting.

**Trade-offs.** No confirmation gate exists yet before a proposed strategy/allocation is shown
to the user on the graph path — same behavior as the legacy `PlanningAgent` today (which also
has no confirmation gate), so this is not a regression, just a gap Phase 7 hasn't closed yet.
Planning's graph-side code path is structurally different from every other domain (a
node-internal branch rather than Task/Evidence/Conclusion), which is a real inconsistency a
future reader needs to know about — hence this entry.

**Future improvements.** Build the dedicated `planning_intake_node`/`planning_strategy_node`
pair described in alternative (2) above, with the 3rd HITL gate (`capital_confirmation`, mirroring
`research_confirmation`/`verification_gate`'s existing pattern in `graph/nodes/human_review.py`)
positioned between them — this is real, scoped work for a follow-up phase, not a quick addition
to the current `_handle_planning` function.

---

## 2026-07-24 — Planner's entity-required short-circuit removed; "market" sentinel entity added

**Problem.** Phase 7.3 (screener_agent's tools) exposed that Planner's short-circuit —
originally `if complexity == "simple" or not entities: skip` — assumed every domain needs a
real ticker to research. Screener's filter-based screens (`tools/screener.py::run`), alert
listing, and mf category/news lookups are whole-market or user-scoped, not per-ticker: they
legitimately have zero entities, but under the old gate they'd never generate a Task at all,
so their ToolSpecs would be unreachable regardless of what Planner's LLM selected.

**Alternatives considered.** (1) Leave the gate as-is and special-case each entity-less
domain in Planner itself (`if intent in {"screener","alert","mf"}: ...`) — rejected: couples
Planner to a hardcoded domain list that has to be kept in sync with the tool catalogue by
hand. (2) Give every entity-less tool its own bespoke "run once, no entity" execution path
outside the normal Task/Send machinery — rejected: reintroduces a second execution
mechanism the RFC deliberately avoided by unifying everything through Task/ToolSpec.

**Final decision.** Removed the `or not entities` clause from Planner's short-circuit
(`graph/nodes/planner.py`) — only `complexity == "simple"` skips now. `_build_tasks` falls
back to a single synthetic entity, `NO_ENTITY_SENTINEL = "market"` (defined in
`graph/tool_catalogue.py`, not `planner.py`, since it's the tools that need to know how to
interpret "no specific entity"), when `entities` is empty — so an entity-less but
non-simple query still produces exactly one Task per tool the LLM selects. Tools that
generally expect a real ticker are unaffected: Planner's own instruction ("pick only tools
whose data the objective actually needs") is what keeps ticker-only tools from being
selected for a "market"-entity task in the first place — the sentinel is opt-in by tool
choice, not forced onto every task.

**Trade-offs.** A previously-free optimization (skip the Planner LLM call entirely for
entity-less non-simple queries) is gone — Planner is now consulted even when there's no
entity, at one extra fast-model call's cost, in exchange for entity-less domains actually
working at all. Separately, and NOT fixed by this change: `graph/nodes/query_analyzer.py`'s
`entities` come from `utils.helpers.extract_all_entities()` unvalidated — generic capitalized
words in a query (e.g. "STOCKS", "PE", "ROE" in "screen stocks with PE below 25 and ROE
above 15") get treated as candidate entities by that function's token-extraction step, same
as it would for `equity_agent.py`, which explicitly re-validates with `validate_ticker()`
before trusting them (`equity_agent.py: valid = [t for t in entities if validate_ticker(t)]`).
QueryAnalyzer does not do this validation, so Planner can occasionally build a wasted Task
against a garbage "entity" for equity-shaped tools too — not new in this phase, just newly
visible while testing screener's entity-less path. execute_task_node already tolerates a
failing/empty tool call by marking the Task `failed` rather than crashing, so this degrades
to "a tool a call that returns nothing useful," not a correctness bug — but it's real,
avoidable API-call waste.

**Future improvements.** Consider having QueryAnalyzer validate `entities` with
`validate_ticker()` (mirroring equity_agent.py's existing precedent) before writing them to
`GraphState`, for any domain, not just screener's entity-less case.

---

## 2026-07-24 — Alert domain on the graph: CRUD-only, `alert_create` deferred

**Problem.** Phase 7.2 of the graph-expansion plan wired `alert_agent.py`'s tools into
`graph/tool_catalogue.py`. Unlike every tool registered so far, alert creation needs
structured parameters (`alert_type`, `threshold`, `condition`) that must be extracted from
free text — `agents/alert_agent.py::_regex_extract`/`_llm_extract` do exactly this slot-filling
today. The graph's `Task`/`ToolSpec` design has no field for this: Planner only ever selects
tool *names* (`graph/nodes/planner.py::_parse_tool_selection` returns `{"tools": [...]}`,
nothing else), and a `Task` carries only `entity` as its one per-task parameter.

**Alternatives considered.** (1) Encode alert parameters into the `entity` string itself
(e.g. `"TCS|price_below|3000"`, mirroring the pragmatic pipe-separated approach used for
`fetch_mf_comparison`'s multi-fund-name entity string) — rejected: alert parameters are
richer (a numeric threshold, an enum type, a free-text condition, a recurring flag) than a
simple list-of-names split, and cramming them into `entity` would make Planner's LLM
responsible for formatting a fragile string it has no schema for. (2) Extend `Task` with a
generic `args: dict` field now, just for alert_create — rejected as solving it twice:
`planning_agent`'s multi-turn intake (Phase 7.3) has the exact same "extract structured
fields from free text" need, so the `args` field design should happen once, informed by both
use cases, not designed narrowly around alerts first.

**Final decision.** Register `alert_list`/`alert_cancel`/`alert_pause`/`alert_resume` now —
none of these need extra structured parameters beyond `entity` (a ticker to match against).
`alert_create` is explicitly NOT registered; `graph/tool_catalogue.py`'s module docstring
documents the gap and points at Phase 7.3 for the real fix (a `Task.args` schema extension
designed alongside planning's intake). `_user_id` (needed by all 4 CRUD tools, since alerts
are scoped to the calling user, not derivable from any tool's own output) is injected into
every task's `context` dict unconditionally by `graph/nodes/research_executor.py::_build_context`,
rather than added as a special-cased dependency — it's state-level data, not a dependency's
computed result.

**Trade-offs.** Alert creation via the graph doesn't work at all yet — a user on the graph
path asking "alert me if TCS drops below 3000" gets no working tool today. This is a real gap
in Phase 7.2's coverage, accepted because forcing a fragile string-encoding workaround would
be worse than an honest gap with a named follow-up. Also: entity-less alert queries (e.g.
"show my alerts", no ticker mentioned) generate zero Tasks under Planner's current
`complexity=="simple" or not entities` short-circuit (`graph/nodes/planner.py`) — that gate
assumes every domain needs an entity, which isn't true for alert-list/mf-news/planning-intake
style queries. Also not fixed here; same Phase 7.3/7.4 follow-up.

**Future improvements.** Phase 7.3: design a `Task.args: dict` extension serving both
`alert_create` and `planning_agent`'s intake, populated by a new extraction step (LLM-driven,
following `utils/helpers.py::llm_resolve_query`'s existing deterministic-first/LLM-fallback
pattern) that Planner or a new dedicated node runs before Task construction. Also revisit
Planner's entity-required short-circuit so alert-list/mf-category/news-only queries actually
generate a Task.

---

## 2026-07-24 — Report file-output lives in CommunicationLayer, not ConclusionBuilder

**Problem.** Phase 7.1 of the graph-expansion plan (extending `graph/tool_catalogue.py`
to cover report_agent's tools) needed to decide where PDF/MD file generation for
report-domain queries happens inside the LangGraph pipeline. The initial plan draft
assumed `ConclusionBuilder` (deterministic, pre-LLM node), on the reasoning that file
generation is a side effect and belongs where Conclusions — not raw evidence — are
finalized.

**Alternatives considered.** (1) `ConclusionBuilder`, per the original plan — rejected
once implementation started: `ConclusionBuilder` runs *before* `CommunicationLayer`'s
LLM call, so `final_report` (the actual markdown text a PDF/MD file needs to contain)
doesn't exist yet at that point — there is nothing to write. (2) A new dedicated
`ReportWriter` node after `CommunicationLayer` — adds a graph node, another edge, and
another place state can get out of sync, for a step that's a few lines of
already-deterministic I/O.

**Final decision.** File output happens inside `CommunicationLayer`
(`graph/nodes/communication_layer.py`), immediately after the LLM call that produces
`final_report`, gated on `state["intent"] == "report"`. `save_report_files()` (extracted
from `agents/report_agent.py::_save_files` into `utils/report_files.py` so the legacy
agent and the graph path share one renderer during the coexistence window) and
`utils/db.py::report_save()` are both plain deterministic I/O, not model calls — calling
them from `CommunicationLayer` doesn't violate its "only node allowed to call the primary
LLM" contract, since the LLM call itself is unchanged; this is ordinary Python running
after that call returns.

**Trade-offs.** `CommunicationLayer` now has a bit more responsibility (LLM synthesis +
chart rendering + conditional file I/O + DB persistence) than the RFC's original
"one clean job" framing implied. Persistence failures are swallowed (logged as a no-op)
so a `reports` table outage never blocks returning the generated report text to the user
— acceptable since the file/DB write is a side effect of an already-successful answer,
not the answer itself.

**Future improvements.** If more domains need post-LLM side effects (screener's CSV
export in Phase 7.3 will), consider factoring a small "post-synthesis side-effects"
helper `CommunicationLayer` calls into, rather than growing per-domain `if` branches
directly in the node body.

---

## 2026-07-23 — Recurring alerts with cooldown, instead of fire-once-only

**Problem.** `alerts` fired once then permanently deactivated
(`active=FALSE`) — flagged in the architecture audit as a real product gap:
a user watching "TCS above 3500" gets exactly one notification even if they
want to be told every time it happens (e.g. a recurring RSI oversold signal
for a swing-trading workflow).

**Alternatives considered.**
- Make all alerts recurring by default — rejected: a condition like
  `price_above` that stays true would fire every 5-minute cron tick with no
  cooldown, spamming Telegram. Fire-once is the safer default.
- Recurring with no cooldown, relying on the condition itself going false
  and true again to naturally re-arm (e.g. only fire `price_above` on the
  crossing, not while-true) — rejected because several alert types
  (`bulk_deal`, `week52_high`) don't have a clean "de-arm" signal to detect
  the condition going false again; a time-based cooldown is simpler and
  uniform across all 14 alert types.

**Final decision.** Added `recurring BOOLEAN DEFAULT FALSE` and
`cooldown_hours INTEGER DEFAULT 24` columns to `alerts` (migration-safe
`ALTER TABLE ADD COLUMN IF NOT EXISTS` in `init_db()`, so existing rows stay
one-shot). `AlertAgent` detects recurring phrasing ("recurring", "every
time", "keep alerting", etc.) at creation time. `scripts/check_alerts.py`
skips evaluation entirely for alerts still inside their cooldown window
(`in_cooldown()`), and on fire, recurring alerts stay `active=TRUE` with
`triggered_at` refreshed instead of deactivating.

**Trade-offs.** Cooldown is per-alert wall-clock time, not condition-aware —
a recurring `price_above` alert could still re-fire on the *same* sustained
breach after 24h even though the price never dipped back below threshold in
between. Accepted as a reasonable default; users who want strict
re-arm-on-reset semantics should use one-shot alerts today.

**Future improvements.** Condition-aware re-arming (only re-fire once the
underlying condition has gone false and become true again) would close the
above gap for `price_above`/`price_below`/indicator-cross types, but not for
level-hit types (`week52_high`, `bulk_deal`) where "re-arm" doesn't have an
obvious analogous reset state.

---

## 2026-07-23 — Conversation summarization instead of silent truncation

**Problem.** `LLMClient`'s per-provider conversation history capped at the
last 20 messages sent to the model — anything older was simply dropped, not
summarized. Flagged as an open gap in the architecture audit: a long chat
thread would silently lose a user's stated goal, a ticker discussed 30 turns
back, or an earlier decision, with no signal to the model that context was
missing.

**Alternatives considered.**
- Increase the window (e.g. cap at 50 instead of 20) — doesn't fix the
  problem, just delays it, and increases per-call token cost/latency for
  every single turn regardless of whether the conversation is actually long.
- Summarize on every call regardless of whether anything overflowed — wastes
  a full extra LLM call per turn even for short conversations that never hit
  the cap.
- Vector-store/embedding retrieval over full history — the codebase has no
  retrieval layer anywhere (per the audit, all "retrieval" is deterministic
  structured-data fetching) and this would be a much larger architectural
  addition for a problem a cheap summarization pass already solves well
  enough.

**Final decision.** New `agents/conversation_memory.py::ConversationMemory`
keeps the same 20-message window sent to the model (no added cost on the
common case) but folds whatever falls out of that window into a running
summary via one `GROQ_MODEL_FAST`/`GEMINI_MODEL` call, *only* the first time
a given chunk overflows (tracked via a cursor, not re-summarized every
turn), prepended as a system message ahead of the recent turns. Also
deduplicates the near-identical history-management code that was previously
copy-pasted between `_GroqProvider` and `_GeminiProvider`.

**Trade-offs.** Adds one cheap LLM call per ~10-message overflow chunk (not
per turn) — small, bounded added latency/cost only for genuinely long
conversations. Summarization failures degrade gracefully to the old
plain-truncation behavior and retry next turn, but a persistently failing
summarizer means that chunk's context is lost, same as before this change.

**Future improvements.** No eval exists for summary quality/faithfulness —
if hallucination-in-summary becomes a concern, would need a small labeled
set to check the summary doesn't drop safety-relevant facts (e.g. portfolio
holdings mentioned early in a planning conversation).

---

## 2026-07-23 — Data confidence tiering + inline citations

**Problem.** Report output stated ratios and directional calls with uniform
confidence regardless of how complete the underlying data actually was.
`_fetch_bundle()` already knew when things were missing (quarterly results
unavailable, 2Y structural levels empty, fundamentals recomputation
incomplete) but that knowledge disappeared into scattered "N/A" markers
instead of shaping the verdict. Separately, `fundamental.py` had already been
computing full ratio derivations (`pe_detail`, `roce_detail`, `de_calc_detail`,
etc.) but they only ever appeared as a static, unconditional appendix
(`calc_breakdown()`) bolted onto the end of every report, disconnected from
the prose that actually used the numbers.

**Alternatives considered.**
- A second LLM call ("self-critique" pass) to check the first response for
  unsupported claims — rejected as double the latency and token cost for a
  problem that's mostly structural (missing data), not reasoning quality.
- Leave `calc_breakdown()` as an always-on appendix and just add the
  confidence line — rejected because it keeps citing every computable ratio
  regardless of relevance, which is noise, not verification.
- A strict schema/function-calling approach where the LLM can only reference
  pre-approved fields — more robust long-term, but a much larger rewrite of
  the prompt/response contract than the gap justified right now.

**Final decision.** Tag the fundamentals text block with `[^key]` footnote
markers (`to_text()`); instruct the model to keep the marker attached when it
states that specific ratio. Render footnotes (`cited_footnotes()`) scoped to
only the markers actually used in the output, so the appendix tracks what was
said instead of dumping everything. Compute a High/Medium/Low confidence tier
per ticker from known gaps in `_fetch_bundle()` and surface it as a
`DATA CONFIDENCE` line the model must acknowledge before giving a verdict.
Exempt cited numbers from the existing hallucination-heuristic
(`_grounding_note()`) since a `[^key]` marker is itself a stronger grounding
signal than a raw digit match.

**Trade-offs.** Citation coverage is limited to the five ratios that already
had a `*_detail` field (P/E, P/B, ROCE, D/E, Current Ratio) — DCF and other
derived figures aren't citable this way (DCF is shown unconditionally instead,
via `dcf_section()`, since it has no per-number marker to attach to). The
model can still choose not to cite a ratio it states, so citation coverage is
a floor, not a guarantee — it improves traceability, it doesn't prove
correctness. Confidence tiering is heuristic (a count of known gaps), not a
calibrated probability.

**Future improvements.** Extend `[^key]` markers to technical-indicator
figures (RSI, MACD, pivot levels) and risk-score components, not just
fundamentals. Consider function-calling / structured output so the model
can only emit a number by referencing a field path, closing the "still didn't
cite" gap entirely. Track citation-rate as a metric over time to see whether
the model's citation discipline holds up as prompts evolve.

---

## 2026-07-23 — Streaming LLM synthesis into the chat UI

**Problem.** Full report synthesis runs at 2,000–3,000 output tokens
(`TOKENS_SYNTHESIS`/`TOKENS_COMPLEX`) and returned as a single blocking call —
10–25 seconds of a frozen chat window per turn, with no indication of
progress beyond a generic loading spinner. Separately, `LLMClient._call()`'s
rate-limit backoff did a blocking `time.sleep()` of up to 16s with nothing
shown to the user in that window, indistinguishable from a hang.

**Alternatives considered.**
- Keep synthesis blocking, just improve the loading indicator (progress bar,
  estimated time) — rejected, doesn't fix the actual latency, only masks it.
- Stream at the orchestrator level for all five specialists uniformly —
  rejected; report/mf/screener/alert/planning responses are consumed as
  finished documents (a saved report, a CSV export, a completed alert), not
  read live turn-by-turn, so streaming them adds complexity with no user-
  facing benefit.
- Client-side fake streaming (chunk the finished response and reveal it on a
  timer) — rejected as dishonest latency theater; doesn't reduce time-to-
  first-useful-token, which is the actual complaint.

**Final decision.** Add a real `stream()` generator to both provider wrappers
(Groq via `stream=True`, Gemini via `generate_content_stream`).
`LLMClient.stream()` keeps the same Groq→Gemini fallback contract as the
non-streaming `_call()`, but only switches providers if the failure happens
*before* any token was yielded — once output is on the wire, a provider
switch would duplicate content for the caller, so a mid-stream failure
surfaces as a trailing inline note instead of a silent restart.
`EquityAgent` gained `process_stream()` / `_answer_stream()` /
`_market_overview_stream()`; the non-streaming versions became thin wrappers
that drain the generator and return its last yield, so there is one
implementation, not two parallel code paths to keep in sync.
`Orchestrator.process_stream()` only streams when routed to `equity`; every
other tab still yields once. `app.py`'s `respond()` re-renders the chat
bubble on each chunk.

**Trade-offs.** More frequent Gradio yields per turn (one per token/chunk
instead of two) — acceptable UI-thread chatter for the latency win, but not
load-tested against many concurrent users. `generate_suggestions()` and
`_persist_turn()` (DB write) still only run after the stream fully drains, so
those remain full-latency operations, just no longer blocking what the user
sees. Streaming is now the load-bearing path for the equity tab — the
non-streaming `process()` is exercised only by tests/direct calls, so a
regression in `process_stream()` breaks both.

**Future improvements.** Extend streaming to `ReportAgent` if report
generation turns are ever surfaced as live chat replies rather than pure
document exports. Add a lightweight client-side "provider: Groq/Gemini"
indicator that updates live, since `provider` can change mid-turn on
fallback. Consider debouncing yields (e.g. every N characters or M
milliseconds) if concurrent-user load testing shows the per-token yield rate
is a bottleneck.

---

## 2026-07-23 — NSE bhavcopy fallback for yfinance

**Problem.** Every price, OHLCV, and fundamentals fetch in the app routes
through a single upstream (`yfinance`, via `tools/data/fetcher.py`). A Yahoo
outage, rate-limit, or schema change takes down equity research, screener, mf
comparison, and the unattended 5-minute alert cron (`scripts/check_alerts.py`)
simultaneously, with no second data path. `get_info()` also had no
try/except around the network call at all — a transient failure surfaced as
a raw traceback in the chat.

**Alternatives considered.**
- A second commercial/free market-data API as full parallel primary source —
  rejected as a much larger integration for redundancy the app doesn't need
  most of the time; NSE's own EOD data (already scraped for bulk/block deals
  in `nse_scraper.py`) covers the "yfinance is down" case specifically.
- Retry-with-backoff only, no alternate source — rejected; doesn't help when
  yfinance itself is down or blocking requests, only helps with transient
  blips.
- Fail loudly and tell the user to retry later — rejected as the worst
  experience for the alert-cron path specifically, where there's no user
  watching to retry anything.

**Final decision.** Wrap yfinance calls in `get_info()`/`get_ohlcv()` in
try/except; when both the NSE and BSE yfinance lookups come back empty, fall
back to that day's NSE bhavcopy CSV (`nse_scraper.get_bhavcopy()`) for a
last-EOD-close quote. Bhavcopy is a full-market CSV, so it's cached once per
calendar day rather than re-fetched per symbol. Fallback results are tagged
`_degraded_source`, which feeds directly into the confidence-tiering decision
above — a bhavcopy-sourced answer is automatically flagged, not silently
presented as equivalent to a live quote.

**Trade-offs.** Bhavcopy is end-of-day only — no intraday price movement, so
`get_ohlcv()`'s fallback is a single synthetic candle, not real history;
multi-day indicators (RSI, MACD, moving averages) will mostly come back
`None` on a degraded fetch rather than computing on stale-extrapolated data
(existing `len(df) < N` guards throughout the codebase already handle this
correctly, since a 1-row df behaves the same as an empty one for those
checks). No fallback exists yet for `get_financials()` (balance
sheet/income/cashflow) — a yfinance outage still blocks fundamentals
recomputation specifically, just not price/quote/chart.

**Future improvements.** Extend the same fallback pattern to
`get_financials()` if a reliable free source for balance-sheet data
surfaces. Consider a background pre-warm of the day's bhavcopy at market
open, rather than fetching lazily on first fallback trigger, to shave the
first-fallback latency. Surface `_degraded_source` in the Gradio UI itself
(not just in the LLM prompt data block) so it's visible even for responses
that don't reach the confidence-tier line.

---

## 2026-07-21 — Shared Postgres (Neon) instead of per-process SQLite

**Problem.** Persistence lived in a local SQLite file (`data/aria.db`), fine
for a single Gradio process but unreachable by `scripts/check_alerts.py`
running as a separate GitHub Actions cron job — alerts created in the app
couldn't be evaluated on a schedule without both processes sharing state.

**Alternatives considered.**
- Have the GitHub Action call an HTTP endpoint on the running app instead of
  reading the DB directly — rejected; requires the app to be always-on and
  publicly reachable, which a local/personal Gradio deployment isn't.
- Ship the SQLite file itself somewhere shared (e.g. a mounted volume or
  periodic sync) — rejected as fragile (write conflicts, staleness) compared
  to a real shared database.
- Redis or another lightweight store just for alert state, keep SQLite for
  everything else — rejected; splits persistence across two systems for no
  real benefit over one shared Postgres instance.

**Final decision.** Migrate all persistence (`portfolio`, `watchlist`,
`alerts`, `reports`, `screeners`, chat sessions) to a single Neon Postgres
database reachable by both the local app and the scheduled checker.
`utils/db.py` rewritten around a `psycopg2.pool.ThreadedConnectionPool` for
the long-lived app process; `scripts/check_alerts.py` uses a single direct
`psycopg2` connection instead, since it's a short-lived process that opens
one connection and exits — a pool would be pointless there.

**Trade-offs.** The app now has a hard external dependency (network reach to
Neon) it didn't have with local SQLite — no persistence at all if the DB is
unreachable, versus SQLite's "always available, just local." Timestamps
became native `TIMESTAMPTZ` instead of `TEXT`, which surfaced latent bugs at
call sites that assumed string slicing (`[:10]`) worked on stored dates.
Repo had to be made public for the GitHub Actions free-minutes tier to cover
a 5-minute cron cadence.

**Future improvements.** Add a connection-health check/retry at app startup
so a transient Neon connectivity issue doesn't crash the whole app on boot.
Consider a local SQLite fallback-cache for read-mostly data (e.g. sector
universe, cached screens) so brief DB outages degrade gracefully instead of
failing every request.

---

## 2026-07-21 — GitHub Actions cron instead of an in-process scheduler for alerts

**Problem.** Alerts (price/indicator/bulk-deal/portfolio-P&L conditions) need
periodic evaluation independent of whether a user has the Gradio app open in
a browser tab.

**Alternatives considered.**
- An in-process background thread/scheduler inside the Gradio app (e.g.
  `APScheduler`) — rejected; only runs while the app process is alive, which
  for a locally-run or low-traffic-hosted Gradio app isn't a reliable
  guarantee, and ties alert reliability to app uptime.
- A dedicated always-on worker process/server — rejected as infrastructure
  overkill and cost for a personal/small-scale tool.

**Final decision.** A standalone script (`scripts/check_alerts.py`) run on a
GitHub Actions cron schedule (every 5 minutes), gated internally by market
hours (`market_is_open()`) since GitHub's cron syntax can't express IST
business hours directly. Runs independently of whether the app itself is
running.

**Trade-offs.** Alert latency is bounded by the 5-minute cron cadence, not
real-time — acceptable for daily-candle-based indicator conditions, less
so if finer-grained (intraday) alerting is ever wanted. GitHub Actions
minutes are a shared, external resource with its own reliability/quota
characteristics outside this project's control. No local dev/offline path
for testing the alert flow without pushing to GitHub.

**Future improvements.** If intraday alerting is added, the 5-minute cadence
against daily-close indicators becomes actively misleading (same value fires
repeatedly through the day) — needs either an intraday data source or an
explicit "daily-close-based" label surfaced to the user. Consider a
dead-man's-switch notification if the cron stops firing (e.g. no successful
run logged in N hours) so a silent GitHub Actions failure doesn't quietly
stop all alerts.

---

## 2026-07-20 — Deterministic keyword routing over an LLM router

**Problem.** A single chat surface needs to route each incoming message to
one of several specialist agents (equity / report / mf / screener / alert /
planning) instead of making the user pick a tab, without adding LLM latency
or cost just to decide *where* a message should go.

**Alternatives considered.**
- Route every message through an LLM call first ("classify this message into
  one of N categories") — rejected as an extra LLM round-trip (latency +
  token cost) on every single turn, for a decision that's usually
  unambiguous from vocabulary alone ("IC report for TCS" vs "SIP for ELSS").
- Route by requiring an explicit tab/command prefix from the user — rejected
  as the friction the single-chat-surface redesign was meant to remove.

**Final decision.** Deterministic keyword/phrase scoring (`route()` in
`agents/orchestrator.py`) checked in priority order (report, screener, mf,
alert, planning, with equity as the catch-all default, since `EquityAgent`
already absorbs general Q&A/portfolio/backtest/market-overview intents on
its own). Multi-word phrases preferred over single ambiguous words to avoid
misrouting (e.g. bare "portfolio" or "cross" would false-positive on
ordinary finance questions). A `last_tab` stickiness rule keeps multi-turn
flows (like `planning`'s conversational intake) from falling through to the
equity catch-all mid-conversation when a follow-up answer has no
category keyword of its own.

**Trade-offs.** Keyword lists require ongoing maintenance as new phrasings
surface (evidenced by three real misroutes found and fixed post-launch: a
substring-match false positive on "BEL", a missing alert-verb stopword, and
bare "portfolio" hijacking generic planning questions). No semantic
understanding — a genuinely novel phrasing with zero keyword overlap always
falls through to equity, silently, rather than asking for clarification.

**Future improvements.** Add a lightweight, cheap-model confidence check only
for messages that score zero across all categories (instead of every
message), as a middle ground between pure keyword matching and a full LLM
router on every turn. Log routing decisions (message → tab) to build a
dataset for tuning keyword lists against real misroutes over time.

---

## 2026-07-20 — Recompute fundamentals from raw statements instead of trusting yfinance fields

**Problem.** yfinance's pre-computed ratio fields (`debtToEquity`,
`returnOnEquity`, etc.) are inconsistently reliable across NSE tickers — most
visibly, D/E is reported as a percentage for some stocks and a raw ratio for
others, with no way to tell which from the field alone.

**Alternatives considered.**
- Trust yfinance's fields directly and move on — rejected; the D/E percentage
  inconsistency alone was enough to produce a materially wrong leverage read
  for an analyst, and other fields carry the same class of risk.
- Pull an entirely separate fundamentals data source — rejected as more
  integration than the problem needed; the raw balance sheet/income
  statement/cashflow data yfinance provides is sufficient to recompute the
  ratios that matter directly.

**Final decision.** `tools/analysis/fundamental.py`'s `compute()` recomputes
D/E, ROCE, Current Ratio, P/E, and P/B directly from raw balance-sheet/
income-statement figures wherever those figures are available, falling back
to yfinance's field (explicitly flagged as needing cross-check, with the
percentage-vs-ratio heuristic applied) only when the raw statement is
missing. Every recomputed ratio carries its derivation as a `*_detail`
string for explainability — the data source of a number is never implicit.

**Trade-offs.** More computation and more surface area for bugs than trusting
upstream fields directly (extraction logic in `_extract_raw()` matches on
row-label keywords across statement formats, which is inherently brittle to
label variations). When the raw statement itself is missing, the code still
falls back to the same potentially-unreliable yfinance field it was built to
avoid — the fix is partial, not universal.

**Future improvements.** This is the natural foundation the `[^key]`
citation work (see 2026-07-23 entry above) builds on — every ratio already
had a "show your work" string before citations existed; extend the same
`*_detail` pattern to any new fundamental ratio added later. Consider
validating recomputed values against the yfinance field and surfacing a
flag when they disagree by more than a threshold, rather than only flagging
when the raw statement is absent entirely.
