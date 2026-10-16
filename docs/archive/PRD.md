# ARIA — Product Requirements Document

**Status:** Draft v1
**Owner:** Arnav Mendiratta
**Last updated:** 2026-07-20

---

## 1. Vision

ARIA (Autonomous Research & Investment Analyst) becomes the go-to research
workspace for Indian equity/mutual-fund analysts and serious retail traders:
one place to ask any question about a stock or fund in plain language, get an
institutional-grade, data-grounded answer, track a portfolio and watchlist,
and — in a later phase — connect a broker account to move from research
straight into (user-confirmed) execution without switching tools.

**This document scopes the full product.** It is intentionally larger than
any single build. It is meant to be read phase-by-phase and fed into
implementation one phase (or even one feature) at a time via the `/goal`
workflow — not built as one monolithic release.

## 2. Problem Statement

Analysts today stitch together 4-6 tools to answer one investment question:
a charting platform, a fundamentals screener, a news aggregator, a broker
terminal, and a spreadsheet for their own notes/portfolio. Each tool is
either free-but-shallow or powerful-but-expensive (Bloomberg/Refinitiv-class
terminals price out most independent analysts and small firms in India).
None of them let you ask a *specific* question ("what's my long-term stop
loss on TCS") and get a direct, numeric, sourced answer — you get generic
dashboards and have to synthesize the answer yourself.

ARIA's current prototype (this repo) already proves the core loop: resolve
a stock from natural language, pull real market/fundamental/technical data,
and have an LLM answer the user's actual question with cited numbers,
instead of a rigid templated report. This PRD extends that from a
single-user local prototype to a subscribable, multi-tenant product.

## 3. Target Users

- **Primary:** Independent equity research analysts and semi-professional
  retail traders in India who currently do manual multi-tool research.
- **Secondary:** Small research/advisory firms (2-20 analysts) wanting a
  shared platform with per-seat accounts.
- **Explicitly out of scope for v1:** institutional buy-side desks needing
  Bloomberg-parity terminal features (order book depth, cross-asset, global
  markets) — NSE/BSE equities and mutual funds only, India only.

## 4. Goals

1. Let a signed-up analyst research any NSE/BSE-listed stock or mutual fund
   through natural-language chat, with every numeric claim traceable to a
   real data source.
2. Persist each analyst's portfolio, watchlist, alerts, and chat history
   privately, isolated from every other tenant.
3. Let an analyst connect their Zerodha account to pull live holdings and
   positions into ARIA's portfolio view (read-only sync) without re-entering
   trades manually.
4. Be architected so that user-confirmed trade execution (Phase 3, see §9)
   can be added without a rewrite — but that capability is explicitly **not**
   built in v1.
5. Run as a real subscription product: signup, login, billing, usage limits
   tied to plan tier.
6. Scale from "runs on a laptop for a demo" to "handles concurrent analysts
   during NSE market hours without degrading" — architecture must support
   this even if v1 infra is small.

## 5. Non-Goals (v1 and near-term)

- **No autonomous/unconfirmed trade execution.** Every order (once execution
  ships in a later phase) requires an explicit per-trade user confirmation
  click. Rule-based or strategy-driven autonomous execution is a distinct,
  much later phase and requires a separate legal/compliance review (SEBI
  Research Analyst / Investment Adviser registration — see §9.3) before any
  design work starts on it.
- No brokers other than Zerodha in the initial broker-integration phase.
- No markets outside NSE/BSE (no US equities, no crypto, no F&O exotic
  structuring beyond what already exists in the prototype).
- No native mobile apps in v1 — responsive web only.
- No becoming a SEBI-registered entity as part of this product build — that
  is a business/legal decision outside engineering scope, but the product
  must be built so that toggling into a compliant posture doesn't require
  re-architecture (audit logging, disclaimers, and RA/RIA-gating hooks are
  in-scope infrastructure even though registration itself is not).

## 6. Success Metrics

- **Activation:** % of signups who complete their first research query
  within 24h of account creation.
- **Retention:** Weekly-active-analyst rate at week 4, week 12.
- **Answer quality:** rate of "cite a number not in the provided data"
  (hallucination) incidents per 1,000 responses, target near-zero, tracked
  via automated post-hoc validation (see §7.9).
- **Latency:** p50/p95 time-to-first-token and time-to-complete-answer for
  chat queries.
- **Cost efficiency:** LLM spend per weekly-active-analyst, tracked per plan
  tier to validate pricing.
- **Broker adoption (Phase 2+):** % of active analysts who connect a broker
  account within 30 days of it becoming available.

---

## 7. Functional Requirements

Numbered by product area. Each item states the requirement; current-repo
status is called out where relevant so `/goal` runs know what already
exists vs. what's net-new.

### 7.1 Authentication & Account Management

- Email/password signup with email verification.
- Login, logout, session management (JWT or server-side session, httponly
  secure cookies).
- Password reset flow (email-based).
- OAuth login (Google at minimum) as a fast-follow, not v1-blocking.
- Multi-factor authentication (TOTP) — required before any broker
  connection is allowed (§7.7), optional otherwise.
- Account settings: change email, change password, delete account
  (with data-export-then-purge flow for compliance).
- **Current repo status:** none of this exists. The prototype has no
  concept of a "user" — `utils/db.py` is a single unscoped SQLite file.
  This is the largest gap between prototype and product.

### 7.2 Multi-Tenancy & Data Isolation

- Every persisted entity (portfolio, watchlist, alerts, chat history, saved
  screeners, reports, broker connections) is scoped to a `user_id`.
- Strict row-level isolation — one tenant must never be able to query
  another's data, enforced at the data-access layer (and ideally via
  Postgres Row-Level Security, not just application-layer filtering).
- Migration path: current SQLite (`utils/db.py`, tables `portfolio`,
  `watchlist`, `alerts`, `reports`, `screeners`) moves to Postgres with a
  `user_id` foreign key added to every table, plus new `users`,
  `sessions`, `subscriptions`, `broker_connections`, `audit_log` tables.

### 7.3 Core Research Chat (Equity, Mutual Fund, Screener, Report tabs)

- Preserve and productionize the four existing research surfaces:
  - **Equity Research** — the adaptive Q&A engine (`agents/equity_agent.py`)
    already built this session: resolve ticker(s) from natural language,
    fetch a full data bundle (fundamentals, 1Y short-term + 2Y long-term
    technicals, risk, sentiment, news), answer the user's literal question
    first, fall back to full report structure for generic asks.
  - **Mutual Fund** (`agents/mf_agent.py`) — fund search, category
    comparison, NAV/investment advice, fund news.
  - **Screener** (`agents/screener_agent.py`) — natural-language +
    filter-based stock screening, top movers, bulk/block deal detection.
  - **Report Generator** (`agents/report_agent.py`) — initiating-coverage,
    result-update, sector-thematic, quick-note report generation with
    PDF/MD export.
- Chat history persisted per user, per research surface, resumable across
  sessions/devices (not just in-memory per Gradio session as today).
- Streaming responses (token-by-token) instead of a single blocking return,
  for perceived latency — current `LLMClient.chat()`/`.ask()` are
  synchronous, full-string returns.
- Every response must be traceable to its inputs: which ticker(s), which
  data timestamp, which LLM call — for both user trust and compliance
  audit (see §7.9, §9.3).

### 7.4 Ticker & Intent Resolution

- Productionize the hybrid resolver built this session
  (`utils/helpers.py::resolve_query`, `extract_all_entities`,
  `validate_ticker`): deterministic alias/regex resolution first, LLM
  fallback only on ambiguity, multi-ticker detection for comparison
  queries, session-continuity for follow-ups ("compare with HDFC bank"
  after "analyse TCS").
- Replace the current `print()`-based terminal tracing
  (`[PARSE]`/`[LLM SYNTHESIS]` lines added this session) with structured,
  queryable observability (see §7.9) — the print-based tracing was a
  debugging aid for local development, not a production observability
  strategy.
- Ticker universe should grow beyond the current curated
  `config.SECTOR_UNIVERSE` (~100 large/mid-cap NSE names) to cover the full
  NSE/BSE listed universe, sourced from an authoritative instrument list
  (see §7.8 data provider requirements) rather than a hand-maintained list.

### 7.5 Portfolio Management

- Manual portfolio entry (add/remove holdings) — exists today
  (`tools/portfolio.py`, `_port_add`/`_port_remove` in equity_agent).
- Live P&L, allocation view (`tools/charts/builder.py::portfolio_chart`,
  fixed this session to return a proper Plotly Figure).
- Per-user portfolio, not the current single-file/single-user model.
- Import portfolio from broker (Phase 2, §7.7) as an alternative/addition
  to manual entry, with clear reconciliation UX when both exist.

### 7.6 Watchlists & Alerts

- Watchlist CRUD (DB table already scaffolded: `utils/db.py` `watchlist`).
- Price alerts (above/below threshold) and indicator-based alerts (e.g.
  "RSI crosses 30 on TCS") — `alerts` table scaffolded, not yet wired to
  any trigger/notification mechanism.
- Notification delivery: email at minimum; push/SMS as fast-follow.
- Background job to evaluate alert conditions on a schedule (needs a
  worker/scheduler — see §8).

### 7.7 Broker Integration — Zerodha (Phase 2)

**Explicitly scoped as research-enhancement, not trading, per §5/§9.**

- Zerodha Kite Connect OAuth flow: user authorizes ARIA to read their
  account.
- Pull holdings, positions, margins, funds into ARIA's portfolio view —
  read-only sync, replacing/supplementing manual portfolio entry.
- Store only what's needed (access token, encrypted at rest — see §8.4),
  never store the user's Zerodha login credentials.
- Token refresh handling (Kite Connect access tokens expire daily — this
  needs a re-auth prompt UX, not a silent failure).
- Disconnect/revoke flow, clearly surfaced in account settings.
- **Order placement is explicitly out of scope for this phase** — see §9
  for the execution phase and its compliance gate.

### 7.8 Data Pipeline

- Current data sources: `yfinance` (unofficial Yahoo Finance wrapper),
  custom NSE bhavcopy/bulk-deal scraper (`tools/data/nse_scraper.py`),
  Google News RSS (`tools/data/news_fetcher.py`), AMFI NAV feed
  (`tools/data/amfi.py`), NSE PDF scraper for quarterly results
  (`tools/data/pdf_scraper.py`).
- **Production risk to flag explicitly:** `yfinance` is an unofficial
  wrapper around a scraped/undocumented Yahoo endpoint. It is fine for a
  single-user prototype; it is a real reliability and ToS risk at
  multi-tenant scale (rate limits, breakage on upstream changes, no SLA).
  Before general availability, evaluate a licensed market-data vendor
  (e.g. NSE's own data feed licensing, Global Data Feed, TrueData, or
  similar India-market data providers) as the primary source, with
  `yfinance` demoted to a fallback or removed.
- Caching layer: current Parquet-file cache (`tools/data/cache.py`) moves
  to a shared cache (Redis) so multiple app instances/workers benefit from
  one fetch, with explicit TTLs tuned to NSE market hours (aggressive
  during 9:15–15:30 IST, longer TTL after close).
- Data freshness must be surfaced to the user — every data-driven answer
  should show "as of" timestamp, especially important once the product is
  used for real decisions.

### 7.9 LLM Orchestration, Observability & Guardrails

- Multi-provider abstraction already exists (`agents/llm_client.py`: Groq
  primary with key rotation, Gemini fallback on rate-limit). Productionize:
  - Per-tenant token/cost tracking, not just global.
  - Configurable per-plan-tier usage caps (§7.10) enforced at the
    `LLMClient` layer.
  - Replace ad-hoc `print()` tracing (added this session for local
    debugging) with structured logs (JSON, correlation-ID per request)
    shipped to a real log/observability backend.
- **Hallucination guardrail:** every synthesis prompt already instructs the
  LLM to only cite numbers present in the supplied data block (built this
  session in `_answer()`'s prompt). Productionize this into an automated
  post-response check — parse numeric claims in the LLM output and verify
  they appear in the source data block, flag/log mismatches, and
  periodically sample for human review. This is the single most important
  trust mechanism for a product giving financial answers.
- Every LLM call's full prompt + response should be retained (encrypted,
  access-controlled) for a defined retention period — both for debugging
  and because financial-research audit trails are a compliance expectation
  even before formal RA/RIA registration is pursued.

### 7.10 Billing & Subscription

- Plan tiers (exact tiers/pricing TBD by business, but the system must
  support at minimum): Free (limited queries/day), Pro (higher limits,
  full feature set), Team/Firm (multi-seat, shared billing).
- Payment gateway integration (Razorpay is the natural choice for an
  India-first product; Stripe as alternative).
- Usage metering tied to the LLM cost tracking in §7.9 — plan limits must
  be enforceable before a request is made (pre-check), not just reported
  after the fact.
- Invoicing, plan upgrade/downgrade, cancellation, dunning for failed
  payments.

### 7.11 Admin & Support Tooling

- Internal admin view: user list, usage/cost per user, ability to
  suspend/refund/adjust plan.
- Basic in-product feedback/support channel (even a simple "report an
  issue" tied to the relevant chat message + its trace ID from §7.9).
- Usage analytics dashboard (aggregate, not per-user PII) for product
  decisions — which research surfaces get used, common query patterns,
  drop-off points.

### 7.12 Notifications

- Transactional email (signup verification, password reset, billing
  receipts, broker token expiry warnings).
- Alert notifications (§7.6).
- Optional daily digest email (market overview + watchlist movers) as a
  retention lever — reuses `_market_overview()` logic already in
  `equity_agent.py`.

---

## 8. Non-Functional Requirements

### 8.1 Performance

- Chat response p95 target: define once LLM streaming (§7.3) is in place;
  today's synchronous full-response model makes any target feel slow
  regardless of backend speed, so streaming is a prerequisite, not a
  nice-to-have.
- Data-bundle fetch (`EquityAgent._fetch_bundle`, built this session:
  parallel-fetches 1Y + 2Y OHLCV, financials, macro, quarterly results,
  news via `ThreadPoolExecutor`) should stay fully parallelized as request
  volume grows — this pattern already exists and should be preserved, not
  regressed, when the fetch layer moves behind an API.

### 8.2 Scalability

- Move from the current single-process Gradio app to a proper split:
  - **API backend** (FastAPI is a natural fit given the existing code is
    already pure Python with clear agent/tool boundaries).
  - **Frontend** as its own deployable (React/Next.js, or a more
    production-grade chat UI framework) — Gradio is fine for the
    prototype demo, not for a multi-tenant product with auth/billing UX.
  - **Background workers** (Celery or RQ + Redis) for: alert evaluation
    (§7.6), report generation/export (§7.3), scheduled screeners, digest
    emails (§7.12) — anything that shouldn't block a chat request.
  - **Database**: Postgres, replacing SQLite (`utils/db.py`), sized for
    concurrent multi-tenant writes.
  - Stateless app servers behind a load balancer so horizontal scaling
    during market-hours peak load is just "add instances."
- Design for NSE market-hours traffic shape: load is concentrated
  9:15–15:30 IST on trading days — the system should handle a peak
  materially higher than its average, not just average-case load.

### 8.3 Availability & Reliability

- Existing LLM fallback (Groq→Gemini on rate-limit,
  `llm_client.py::_call`) is a good pattern — extend the same
  fallback/circuit-breaker philosophy to the market-data layer (§7.8):
  if the primary data vendor fails, degrade gracefully (cached/stale data
  with a visible "data may be stale" flag) rather than erroring the whole
  response.
- Target uptime SLA to be set by business, but the architecture must not
  have single points of failure in the request path once past prototype
  stage (single SQLite file, single unqueued LLM call, single data
  source are all current single-points-of-failure to eliminate).

### 8.4 Security

- Secrets (LLM API keys, broker tokens, DB credentials) in a proper
  secrets manager (AWS Secrets Manager, Vault, or equivalent) — not `.env`
  files, which is fine for local prototype only.
- Broker access tokens (§7.7) encrypted at rest, decrypted only in-memory
  at point of use.
- Standard web app security: HTTPS everywhere, CSRF protection, rate
  limiting on auth endpoints, input validation/sanitization on every
  user-supplied field (the current ticker/query parsing already does
  significant validation for correctness — extend the same rigor to
  security, e.g. no raw user input ever reaches a SQL query unparameterized).
- Dependency/vulnerability scanning in CI (see §10).

### 8.5 Data Privacy & Compliance Posture

- Financial data + eventually broker-linked data is sensitive — treat with
  PII-grade handling even before any formal compliance program: data
  minimization, encryption at rest and in transit, defined retention
  periods, user-initiated data export/delete.
- Every AI-generated research answer carries a visible disclaimer
  ("not SEBI-registered investment advice" — already present as a footer
  line in today's prompts, e.g. `equity_agent.py`'s report templates —
  keep and standardize this across every surface, not just some).
- See §9.3 for the SEBI RA/RIA registration discussion specific to any
  future execution phase.

### 8.6 Cost Management

- Per-tenant LLM cost visibility (§7.9) feeds directly into whether the
  plan-tier pricing (§7.10) is sustainable — this is a product-economics
  requirement, not just an engineering one.
- Alerting on anomalous usage (a single tenant spiking cost) before it
  becomes a billing surprise.

### 8.7 Disaster Recovery

- Automated Postgres backups with a defined RPO/RTO once real user data
  (portfolios, broker connections) exists — this is materially higher
  stakes than the current local-SQLite prototype.

### 8.8 Internationalization

- **Non-goal for v1**: India-only (NSE/BSE, INR, IST) by design, matching
  the existing `config.py` constants (`IST` timezone, `.NS`/`.BO` ticker
  suffixes). Do not build generic multi-market abstractions speculatively.

---

## 9. Phased Rollout Plan

### Phase 0 — Current state (this repo)
Single-user local prototype. Gradio UI, SQLite, four research agents,
adaptive Q&A engine for equity research (built this session), hybrid
ticker/intent resolution, chart-contract bug fixes. No auth, no
multi-tenancy, no broker integration, no billing.

### Phase 1 — MVP SaaS (research-only)
Goal: first paying analyst can sign up, log in, and use the four research
surfaces with their own private data.
- §7.1 auth, §7.2 multi-tenancy migration (SQLite→Postgres + user
  scoping), §7.10 billing (at least one paid tier), §7.3 chat history
  persistence, §8.2 API/frontend split enough to support real auth UX
  (Gradio's auth story is not built for multi-tenant SaaS).
- Keep all four existing agents' research logic largely as-is — the value
  is in the agents already built; Phase 1 is primarily infrastructure
  around them, not new research capability.

### Phase 2 — Broker connect (read-only)
Goal: analyst connects Zerodha, sees real holdings inside ARIA.
- §7.7 Zerodha Kite Connect OAuth + read-only portfolio/positions sync.
- §7.6 alerts go live (needs the background worker infra from §8.2 either
  way, natural pairing with this phase).
- §7.8 evaluate/adopt a licensed data vendor if usage has outgrown
  `yfinance`'s reliability envelope by this point.

### Phase 3 — Confirmed-execution (gated)
Goal: analyst can place an order through ARIA, always with an explicit
per-trade confirmation step — never autonomous.
- **Hard prerequisite, not an engineering task:** legal review of whether
  ARIA's research outputs + a "place this order" button constitutes
  investment advice requiring SEBI RA/RIA registration, and under what
  operating model (e.g. execution-only tool where the user makes their own
  decision vs. an advisory service) this can launch compliantly. This PRD
  does not resolve that question — it flags that Phase 3 does not start
  engineering until it's resolved.
- Order placement via Kite Connect, full audit trail (§7.9's LLM audit
  trail extends naturally to an order audit trail: what data/answer led to
  the order, exact confirmation timestamp, exact order parameters).
- Order status tracking, position reconciliation post-fill.

### Phase 4 — Scale & harden
Goal: the product survives real concurrent multi-tenant load without the
Phase 1-3 infrastructure choices becoming bottlenecks.
- Load-test around NSE market-hours traffic shape (§8.2).
- Multi-broker expansion if user demand supports it (§5 non-goal for v1,
  revisit here).
- Formal SLA, on-call/incident process, DR drills (§8.7).

### Explicitly deferred beyond this roadmap
Autonomous/unconfirmed trade execution, strategy-driven auto-trading,
non-Indian markets, native mobile apps — all require their own PRD once
the phases above are live and there's real usage data to justify them.

---

## 10. Technical Architecture (directional, not prescriptive)

This section gives shape for `/goal` planning, not final decisions —
detailed architecture should be its own design pass per phase.

- **Today:** monolithic Python process, Gradio serves UI + handles
  requests synchronously, SQLite file on disk, agents call tools directly
  in-process.
- **Target shape by Phase 1 completion:**
  - FastAPI backend exposing the four agents' `process()`-equivalent logic
    as authenticated, tenant-scoped endpoints (the existing
    `agents/*.py` business logic is largely reusable as-is — the
    Gradio-specific glue in `app.py` is what gets replaced).
  - Separate frontend (chat UI + auth + billing screens).
  - Postgres for all persistent state.
  - Redis for cache (§7.8) + job queue backing (§8.2).
  - Worker process(es) for background jobs (alerts, reports, digests).
  - Containerized (Docker) services, CI/CD pipeline (§10.1), deployed to a
    cloud provider (choice open — AWS/GCP/Azure all viable given India
    data-residency needs should be checked against provider region
    offerings).

### 10.1 Engineering Practices Needed for "Production Grade"
- CI: lint, type-check, dependency/security scan, and — critically, since
  `CLAUDE.md` notes **no test suite currently exists** — this must change
  before Phase 1 ships. Unit tests for the deterministic logic (ticker
  resolution, data computation in `tools/analysis/`) are straightforward
  to add now; LLM-output testing needs a different strategy (golden-set
  regression prompts + the hallucination-guardrail checks from §7.9).
- CD: automated deploy pipeline, staged environments (dev/staging/prod),
  not direct-to-prod pushes.
- Infra as code (Terraform or equivalent) once beyond a single manually
  configured server.

---

## 11. Data Model Additions (Phase 1 baseline)

New tables beyond the existing `utils/db.py` schema
(`portfolio`, `watchlist`, `alerts`, `reports`, `screeners`):

- `users` — id, email, password_hash, email_verified, mfa_secret, created_at
- `sessions` — id, user_id, token, expires_at
- `subscriptions` — id, user_id, plan_tier, status, billing_provider_ref
- `broker_connections` (Phase 2) — id, user_id, broker, encrypted_access_token, expires_at, connected_at
- `audit_log` (Phase 1, extended in Phase 3) — id, user_id, event_type, payload, trace_id, created_at
- Every existing table (`portfolio`, `watchlist`, `alerts`, `reports`,
  `screeners`) gets a `user_id` foreign key.

---

## 12. Third-Party Integrations Required

- **Zerodha Kite Connect** — broker OAuth + data (Phase 2), order
  placement (Phase 3).
- **Payment gateway** — Razorpay (or Stripe) for §7.10.
- **Transactional email** — SendGrid/AWS SES for §7.12, §7.1.
- **Market data vendor** — evaluate for §7.8 before GA (yfinance is
  prototype-only, not a production dependency to ship broadly on).
- **Push/SMS** (fast-follow) — Twilio/FCM for §7.6 alert delivery beyond
  email.
- **Observability stack** — structured logging + error tracking (e.g.
  Sentry) + metrics/APM for §7.9, §8.1.

---

## 13. Risks & Open Questions

- **yfinance reliability at scale** (§7.8) — biggest technical risk to
  resolve before broad GA; needs a licensed-data-vendor decision.
- **SEBI RA/RIA registration** (§9.3) — biggest business/legal risk;
  blocks Phase 3 entirely until resolved, and even Phase 1's research
  outputs should carry compliant disclaimers now to avoid ambiguity later.
- **LLM cost at scale** — needs real usage data from Phase 1 to validate
  plan-tier pricing (§8.6) before broad marketing spend.
- **Hallucination risk in a financial-advice-adjacent product** — highest
  product-trust risk; §7.9's guardrail is necessary but should be treated
  as an evolving investment, not a one-time build.
- **Data residency** — if targeting India-based analysts specifically,
  confirm cloud provider region choice satisfies any applicable data
  localization expectations for financial data.

---

## 14. Appendix — What Already Exists (Phase 0 inventory)

For `/goal` runs to know what's reusable vs. net-new:

| Area | File(s) | Status |
|---|---|---|
| Equity adaptive Q&A | `agents/equity_agent.py` | Built this session, single-user |
| Ticker/intent resolution | `utils/helpers.py` | Built this session, single-user |
| Mutual fund agent | `agents/mf_agent.py` | Exists, single-user |
| Screener agent | `agents/screener_agent.py` | Exists, single-user |
| Report generator | `agents/report_agent.py` | Exists, single-user |
| LLM provider abstraction | `agents/llm_client.py` | Exists, no per-tenant tracking |
| Charts | `tools/charts/builder.py` | Fixed this session (Figure/None contract) |
| Data fetchers | `tools/data/*.py` | Exists, yfinance-dependent |
| Technical/fundamental/risk analysis | `tools/analysis/*.py` | Exists, reusable as-is |
| Portfolio | `tools/portfolio.py`, `utils/db.py` | Single-user SQLite |
| Persistence | `utils/db.py` | SQLite, no multi-tenancy |
| Auth | — | Does not exist |
| Billing | — | Does not exist |
| Broker integration | — | Does not exist |
| Background jobs | — | Does not exist |
| Test suite | — | Does not exist (noted in `CLAUDE.md`) |
