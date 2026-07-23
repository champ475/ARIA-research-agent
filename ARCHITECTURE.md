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
