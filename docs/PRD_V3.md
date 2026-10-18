# ARIA v3 PRD — Trustworthy Research Companion

Oct 18, 2026 · @Arnav

> Offline snapshot of the living Claude Doc: https://claude.ai/code/artifact/865a4490-cc0f-4ff5-afb6-62c022abec12. Re-export it from the doc if the doc has changed. Status columns here are a point-in-time copy; the doc is the tracker.

## Overview

ARIA v3 makes every answer trustworthy first, then turns ARIA from a chat that answers single questions into a companion that keeps track of what a user owns and watches. Work ships in four phases, ordered by priority: P0 correctness, P1 continuity, P2 product surfaces, then scale.

**Problem.** v2 is stable (2,561 tests pass; 0 errors, 0 empty LLM replies and 0 gates in the latest live runs) but not yet trustworthy. In 30 live probe turns on 2026-10-18, 4 answers were factually wrong, 2 crossed the education-only boundary, and 10 carried warning notes, most of them false. Follow-up questions break, latency is 25.9 s at p95 on the production database, and the eval suite cannot see any of this because it stops at the Planner.

**Goals**

1. Zero wrong figures or claims on a reference answer set, and under 1 per 100 live answers.
2. Zero advice-boundary breaches.
3. Follow-ups answered correctly 90% of the time (95% by v3.0).
4. Turn p95 under 12 s cold (v2.2) and under 10 s (v3.0).
5. Surfaces beyond chat: stock, fund and portfolio pages and a daily digest.
6. A free tier that a real research thread does not exhaust, and a paid tier that covers its cost.

**Non-goals**

- Personalised buy/sell advice, portfolio recommendations or price predictions (ARIA stays education-only; `docs/ADVICE_POLICY.md`).
- Order placement or broker integration.
- Markets outside NSE/BSE, crypto, derivatives (F&O).
- Native mobile apps (the web front end must work at 375 px).

**Sources.** This PRD turns the 2026-10-18 review into requirements: [ARIA v2 — Product & AI Engineering Review, and v3 Plan](https://claude.ai/code/artifact/6b6718f8-a692-4d54-96f5-84e77af58974), plus the open items in `docs/smoke/2026-10-18-consolidated-issues.md`. It follows the production-readiness PRD (Phases 0–3, done) and uses new requirement ids so the two never collide: CORR (correctness), EVAL, CONV (conversation), PERF, SURF (surfaces), FEED (data), PLAT (platform), BIZ.

## Users and product principles

The primary user is an Indian retail investor who holds a few stocks and mutual funds, follows a watchlist, and wants to understand a company or fund before acting elsewhere. There are no real users yet, so the 20-person pilot in Phase 3 is also the first source of usage data.

| Persona | Needs | Today's pain (from the review) |
| --- | --- | --- |
| Learner: new investor, SIPs, a few stocks | Plain explanations, correct rules (tax, ELSS, SIP), no jargon | Stale LTCG rules; warning notes on correct worked examples |
| Tracker: 5 to 20 holdings plus a watchlist | "How are my holdings doing", alerts, a daily view | Portfolio cannot be changed from chat; no digest; alerts without confirmation |
| Researcher: compares companies and funds | Correct tables, peers, follow-up questions, reports | Wrong comparative claims; follow-ups refused; the "full report" is a snapshot |
| Operator (the owner) | Cheap to run, easy to debug, safe to change | Evals blind to answer quality; tool spans unnamed; 1.5k-character traces |

**Principles every requirement follows**

1. **Correct or silent.** A figure or claim ARIA cannot back with fetched data is left out or named in a note; it is never guessed.
2. **One note, one claim.** A warning names the claim it is about; generic notes are not allowed.
3. **Education, never a pick.** No buy/sell, "upside", "bullish", "suitable for your portfolio" or personal suitability, whatever the user asks.
4. **Conversation remembers.** A follow-up is answered from what is already on screen before any new research.
5. **Every write is confirmed.** Alerts, portfolio and watchlist changes show a confirm card with Edit and Cancel.
6. **Fast is a feature.** A cached question answers in under 4 s; a cold research turn in under 12 s.
7. **Measured, not assumed.** Each phase ships only when its gate metric, measured by the eval suite or the `turns` table, passes.

## Success metrics

Each phase is judged on the metrics below; a phase's gate names which ones must pass before it ships. Baselines come from the 2026-10-18 probe (30 turns) and LangSmith (104 turns, 17–18 Oct).

| Metric | Baseline | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Measured by |
| --- | --- | --- | --- | --- | --- | --- |
| Wrong figures or claims | 4 in 30 answers | 0 on answer eval | 0 on answer eval | under 1 per 100 live | under 1 per 100 live | EVAL-01, weekly trace sample |
| Advice-boundary breaches | 2 in 30 | 0 | 0 | 0 | 0 | EVAL-01, `finalize` counters |
| Answers with a warning note | 10 of 30 | under 10% | under 5% | under 3% | under 3% | `turns.grounding`, caveat counter |
| Follow-up success | not measured | measured | 90% | 95% | 95% | EVAL-01 follow-up family |
| Turn p95, cold, production database | 25.9 s | — | under 12 s | under 10 s | under 10 s | `turns`, `docs/SLO.md` |
| Turn p50, cached | 2.5 s (local DB) | — | under 4 s | under 3 s | under 3 s | `turns` |
| LLM calls per research turn | 3.3 | — | 2.5 | 2.0 | 2.0 | `turns.llm_by_node` |
| Live judge flag rate | 41% before PR #39 | tracked | under 10% | under 10% | under 10% | LangSmith weekly sample |
| Thumbs-down rate | not on traces | — | baseline set | under 10% | under 8% | LangSmith feedback (PLAT-02) |
| Pilot week-2 retention | no users | — | — | 40% (20 users) | 40% | product analytics (SURF-07) |
| Concurrency ratio, 20 users | 1.88x | — | — | under 1.5x | under 1.5x | `loadtests/run.py` |

## Phases at a glance

Phases follow priority: P0 correctness, P1 continuity and speed, P2 polish plus new surfaces, then scale. A phase starts when the previous gate passes; work inside a phase can run in parallel.

| Phase | Priority | Requirements | Gate to leave the phase |
| --- | --- | --- | --- |
| 1 — v2.1 Correct | P0 | CORR-01 to CORR-07, EVAL-01, EVAL-02 (9) | EVAL-01: 0 wrong figures, 0 advice breaches; notes under 10% |
| 2 — v2.2 Continuous | P1 | CONV-01 to CONV-09, PERF-01 to PERF-06, PLAT-01, PLAT-02, EVAL-03 (18) | p95 under 12 s; follow-ups 90%; notes under 5% |
| 3 — v3.0 Companion | P2 + new | SURF-01 to SURF-08, FEED-01 to FEED-04, PLAT-03, BIZ-01, BIZ-02 (15) | 2-hour soak flat; under 1.5x at 20 users; pilot week-2 retention 40% |
| 4 — v3.1 Scale | — | BIZ-03, BIZ-04, EVAL-04, EVAL-05, PLAT-04, PLAT-05 (6) | Paid tier live; wrong-claim rate under 1/100 for 4 weeks; cost within budget |

Estimates: Phase 1 about two weeks, Phase 2 three to four, Phase 3 six to eight; Phase 4 is re-planned from pilot data.

## Phase 1 — v2.1 Correct (P0)

Phase 1 removes every known wrong answer and advice breach and builds the answer-level eval that gates all later phases. Estimate: about two weeks. No new features ship until its gate passes.

**Gate:** EVAL-01 shows 0 wrong figures or claims and 0 advice breaches; note rate under 10% on the eval set; `make eval` and `judge_calibration.py` green.

| ID | Requirement | Acceptance criteria | Files | Status |
| --- | --- | --- | --- | --- |
| CORR-01 | Read dividend yield from the right field. Today `trailingAnnualDividendYield` (0 for most NSE stocks) wins over `dividendYield` | ITC, SBIN, RELIANCE, INFY show the `dividendYield` value (or `dividendRate / price`); 0 or missing prints "not reported", never 0.0%; screener `div_yield` uses the same rule; golden test per field | `graph/nodes/conclusion_builder.py::_QUOTE_TWIN`, `graph/quote_fields.py`, `tools/screener.py::metrics_from_info`, `tools/analysis/fundamental.py` | Not started |
| CORR-02 | Grounding understands Unicode signs and grouping | `–`, `‑`, `‒`, `−` before a number count as minus; thin space, NBSP and narrow NBSP inside digits are grouping ("4 436" = 4436); `check("–4.54 %", "-4.54")` is clean; cases in `tests/test_grounding.py` | `graph/grounding.py` | Not started |
| CORR-03 | Check comparative and directional claims in every answer | New `graph/claims.py` extracts "highest / lowest / largest / leads / above all / cheaper than" claims over named entities and a metric, and verifies them against the turn's evidence in code; a false claim is rewritten (non-streamed) or gets a named note (streamed); the SBIN market-cap and TCS/Wipro dividend probes are caught; 0 extra LLM calls | `graph/claims.py` (new), `agents/graph_orchestrator.py`, `graph/nodes/communication_layer.py` | Not started |
| CORR-04 | Close advice-boundary gaps on input and output | Analyzer schema gains `advice_request: none \| general \| personal_pick`; personal picks reframe in code. Refuse or reframe "suggest good small caps", "best stocks to buy now", crypto buy questions. `finalize` removes "room for upside", "bullish", "upside potential of X%", "suitable for a long-term portfolio"; analyst targets shown as data only. Cases in `tests/test_guardrails.py` and the golden set | `graph/guardrails.py`, `graph/advice_policy.py`, `graph/schemas.py`, `prompts/query_analyzer.system`, `prompts/communication.system` | Not started |
| CORR-05 | Dated rules pack for concept answers | `data/rules/` holds tax (LTCG, STCG, dividend tax), ELSS lock-in, SIP mechanics, SEBI fund categories, each with source link and review date; concept intents retrieve matching entries as evidence; LTCG answer says 12.5% above ₹1.25 lakh and 20% short-term and cites the pack; a row in `docs/DATA_SOURCES.md`; a test fails when any entry is past its review date | `data/rules/` (new), `graph/nodes/communication_layer.py`, `prompts/communication.direct` | Not started |
| CORR-06 | Field plausibility contracts and a data canary | `tools/data/contracts.py` adds range and "mostly zero" checks per field (dividend, ROE, D/E, P/E, margins); `scripts/data_canary.py` alerts when a field is 0 or null for over 50% of a 50-symbol sample; ops alert plus `docs/RUNBOOK.md` section | `tools/data/contracts.py`, `scripts/data_canary.py`, `utils/watchdog.py` | Not started |
| CORR-07 | Illustrative numbers in concept and backtest answers are not flagged | Worked examples are labelled "Example:" by the prompt and skipped by grounding; backtest figures match their evidence (after CORR-02); XIRR and LTCG probes carry no "Unverified figures" note | `graph/grounding.py`, `prompts/communication.direct` | Not started |
| EVAL-01 | Answer-level eval suite that gates CI | About 60 cases with reference facts across equity, compare, peers, concept, tax, MF, screener, alerts, follow-ups and advice probes (all 30 review probes included); metrics: numeric accuracy (code), claim correctness (CORR-03 + model), advice breaches, note rate, follow-up success, length; replayable fixtures; CI fails on any wrong figure or breach | `evals/answers/` (new), `evals/runner.py`, `.github/workflows/ci.yml` | Not started |
| EVAL-02 | Calibrate the judge on live traces | A weekly script samples 50 live judged turns from LangSmith into `evals/cases/judge_labels.jsonl` for labelling; live flag rate is a tracked metric next to precision and recall | `evals/judge_calibration.py`, `scripts/` (new sampler) | Not started |

Prompt changes in CORR-04, CORR-05 and CORR-07 invalidate recorded fixtures: re-record with `python evals/runner.py --record`, then `make eval-baseline`.

## Phase 2 — v2.2 Continuous (P1)

Phase 2 fixes the second and third message of a conversation, makes every write safe, and cuts cold latency by half. Estimate: three to four weeks.

**Gate:** follow-up success 90% and note rate under 5% on EVAL-01; cold turn p95 under 12 s on the production database over 50 turns; LLM calls per research turn at most 2.5; all new e2e journeys green.

### Conversation and writes

| ID | Requirement | Acceptance criteria | Files | Status |
| --- | --- | --- | --- | --- |
| CONV-01 | Answer follow-ups from the last turn first | State keeps the last turn's evidence and conclusions per chat; a "from context" route before the Planner answers when they suffice; "which one has the lowest PE?" after a 6-bank peer table answers with 0 tools in under 3 s; the 5-company cap applies only to new research | `graph/state.py`, `graph/nodes/query_analyzer.py`, `graph/nodes/planner.py`, `graph/graph.py`, `agents/graph_orchestrator.py` | Not started |
| CONV-02 | Follow-up chips from graph state | Chips use the resolved entity and final intent, not `_TICKER_RE`; concept answers get concept chips ("XIRR vs IRR", "Calculate my SIP XIRR"); BYO suggestion call gets reasoning headroom and logs failures | `agents/graph_orchestrator.py::suggestions_for`, `generate_suggestions` | Not started |
| CONV-03 | One confirm card for every write | Alert create, cancel, pause and resume, portfolio add/remove, watchlist add/remove show Create / Edit / Cancel before running; "confidence 1.00" never shown; the far-from-price guard stays | `graph/confirmation.py`, `graph/write_policy.py`, `graph/nodes/human_review.py`, `ui/chat.py` | Not started |
| CONV-04 | Change portfolio and watchlist from chat | New write tools `portfolio_add`, `portfolio_remove`, `watchlist_add`, `watchlist_remove` (`side_effect=True, cacheable=False`), behind CONV-03; "Add 10 shares of TCS at 3500" adds the lot after confirm; tenant-isolation cases added | `graph/tool_catalogue.py`, `services/portfolio.py`, `services/watchlist.py`, `tests/integration/test_tenant_isolation.py` | Not started |
| CONV-05 | Clear scope replies | US and other foreign listings (Apple, Tesla), crypto and F&O get one sentence on what ARIA covers plus a useful chip; no fuzzy Indian matches for well-known foreign names; no planning intake for crypto | `graph/entity_resolution.py`, `graph/guardrails.py`, new `data/instruments/foreign_names.csv` | Not started |
| CONV-06 | Planning uses the right goal and asks what it needs | Planning names the saved profile and goal it is using and asks before reusing one from another chat; asks retirement age for retirement goals (D7) | `graph/nodes/planning_node.py`, `prompts/planning.intake` | Not started |
| CONV-07 | Timeframe travels in state | `with_timeframe` no longer appends words to the query; the chart tool and the prompt read `state.timeframe`; chart title matches; no "weekly" echo (T-4, B-3) | `services/chat.py::with_timeframe`, `graph/state.py`, `tools/charts/builder.py` | Not started |
| CONV-08 | Note policy: one note, one claim | Generic "Could not fully confirm every figure" removed; every note names its claim or figure; a single-figure answer whose figure is in evidence carries no note; note rate under 5% on EVAL-01 | `graph/caveats.py`, `graph/grounding.py`, `graph/nodes/verification.py` | Not started |
| CONV-09 | Honest gaps offer a next step | When ARIA lacks data (sectors, history beyond range), the answer names what it can show and adds a chip for it | `graph/nodes/communication_layer.py`, `prompts/communication.system` | Not started |

### Speed

| ID | Requirement | Acceptance criteria | Files | Status |
| --- | --- | --- | --- | --- |
| PERF-01 | Unlock the composer before saving | `respond` yields the final answer, then persists; persist failures still logged; composer unlocks 1 to 2 s sooner on Neon | `ui/chat.py::respond`, `services/chat.py::persist_turn` | Not started |
| PERF-02 | Database close to users | Neon project in an Asia region (owner task); session lookup cached for 60 s per token; persist under 300 ms p95 | `utils/db.py`, `web/session.py`, owner TODO | Not started |
| PERF-03 | Prefetch and overlap tools | `fetch_info` and `fetch_ohlcv` start when the analyzer resolves a ticker, in parallel with the Planner; second-wave `compute_*` runs per entity as soon as its inputs land | `graph/nodes/research_executor.py`, `graph/nodes/execute_task.py`, `graph/graph.py` | Not started |
| PERF-04 | Fewer LLM calls per turn | One merged analyzer-planner call for simple and moderate turns; Planner skipped for fixed-tool intents (price, alerts, reports list, MF category); judge skipped when every evidence item is a cache hit already judged | `graph/nodes/query_analyzer.py`, `graph/nodes/planner.py`, `graph/nodes/verification.py` | Not started |
| PERF-05 | Schema-constrained JSON | Analyzer, planner, judge and reflection request JSON with a schema on Groq and Gemini; `parse_failures` near 0 over 100 turns; repair retry kept only as fallback | `agents/llm_client.py`, `utils/llm_json.py` | Not started |
| PERF-06 | Warm cache for popular symbols | The nightly worker job refreshes `fetch_info`, `fetch_financials` and `fetch_ohlcv` evidence for the top 200 symbols; first question of the day on those is a cache hit | `worker.py`, `tools/data/evidence_cache.py` | Not started |

### Reliability and observability

| ID | Requirement | Acceptance criteria | Files | Status |
| --- | --- | --- | --- | --- |
| PLAT-01 | No silent failures | Database down: the chat shows "ARIA can't reach its database, try again in a moment" and an ops alert fires (E-2); after a websocket reconnect the active chat reloads from `chat_history` (E-3); RUNBOOK sections added | `web/session.py`, `ui/chat.py`, `utils/watchdog.py`, `docs/RUNBOOK.md` | Not started |
| PLAT-02 | Traces that answer questions | Tool spans named `tool:<name>` with `cache_hit`, `entity`, `source`; thumbs up/down sent as LangSmith feedback on the turn's run; full redacted prompt and answer stored on `turns`; trial-limited turns tagged `limited`; one log line per tool call | `utils/tracing.py`, `graph/nodes/execute_task.py`, `services/chat.py::record_like` | Not started |

### Tests

| ID | Requirement | Acceptance criteria | Files | Status |
| --- | --- | --- | --- | --- |
| EVAL-03 | Close the e2e gaps the UI smoke found | Seeded user with 20+ chats and long answers (sidebar visible, nothing over `#app-bar`); 1536×730 viewport; feedback panel open/close; Number fields valid on load; database-down and reconnect journeys; axe stays at 0 serious | `tests/e2e/` | Not started |

## Phase 3 — v3.0 Companion (P2 and new surfaces)

Phase 3 adds the pages and the daily digest that make ARIA worth opening without a question, replaces scraped data where it hurts most, and runs a 20-person pilot. Estimate: six to eight weeks; the front end and the data feed can run in parallel.

**Gate:** a 2-hour soak with flat memory; concurrency ratio under 1.5x at 20 users; 20 pilot users with week-2 retention of 40%; thumbs-down under 10%; all Phase 1 and 2 metrics still green.

### Surfaces

| ID | Requirement | Acceptance criteria | Files | Status |
| --- | --- | --- | --- | --- |
| SURF-01 | A lighter web front end | React or HTMX pages over the existing FastAPI routes; answers stream over SSE; same `--aria-*` tokens; chat, sidebar, watchlist, alerts, portfolio, settings, admin at parity; axe 0 serious; no sideways scroll at 375/768/1280 px; Gradio removed after parity. Decision recorded in `ARCHITECTURE.md` | `web/`, new `frontend/`, `server.py`, `services/` (unchanged contract) | Not started |
| SURF-02 | Stock page | `/stock/<SYMBOL>`: snapshot, chart with timeframe, key ratios with footnotes, peers, recent filtered news, alerts on it, "Ask about this stock" opening a chat with it in context | `frontend/`, `services/` (new stock service), `graph/` tools reused | Not started |
| SURF-03 | Fund page | `/fund/<scheme>`: NAV chart, 1/3/5-year and rolling returns, TER, AUM, volatility, category rank, "compare with" | `frontend/`, `services/`, FEED-03 | Not started |
| SURF-04 | Portfolio insights | Allocation by stock, sector and asset type; P&L per lot and total; XIRR; concentration warnings phrased as facts ("TCS is 42% of your holdings"), never advice; CSV import kept | `tools/portfolio.py`, `services/portfolio.py`, `frontend/` | Not started |
| SURF-05 | Daily digest | Opt-in Telegram or email after market close: watchlist moves, fired alerts, holdings P&L, results dates; one worker job with heartbeat; unsubscribe link; no LLM call unless the user has a BYO key | `worker.py`, `utils/mailer.py`, Telegram module, `docs/PRIVACY.md` | Not started |
| SURF-06 | A real research report | Template: business overview, 5-year revenue and profit trend, margins, balance sheet, peers, valuation range, risks, recent news, sources; PDF works in Docker and the link is hidden where it cannot render | `graph/nodes/output_files.py`, `prompts/` (report prompt), `utils/report_files` | Not started |
| SURF-07 | Pilot and product analytics | Privacy-respecting events (turn intent, follow-up, chip click, page view, thumbs) in Postgres; a weekly retention and funnel query; 20 invited pilot users; consent text in `docs/PRIVACY.md` | new `utils/analytics.py`, migration, `services/admin.py` | Not started |
| SURF-08 | Readability polish | Indian grouping and no decimals on crore (₹8,88,554 cr) through one formatter (D3); indicators to 1 decimal with a one-line meaning; screener says "N of M stocks match, sorted by X" (D5); reply in the user's script (Hinglish stays Latin); empty-portfolio reply has no market line or research footer (D6); EMMVEE sector checked after refresh (D4) | `utils/money.py`, `graph/evidence_render.py`, `tools/screener.py`, `prompts/communication.system` | Not started |

### Data

| ID | Requirement | Acceptance criteria | Files | Status |
| --- | --- | --- | --- | --- |
| FEED-01 | Licensed fundamentals and corporate actions | A vendor feed behind `providers/market_data()`; Yahoo becomes fallback; dividends, splits and results dates from corporate actions; contract tests and `docs/DATA_SOURCES.md` row with licence terms | `tools/data/providers/`, `tools/data/contracts.py`, `docs/DATA_SOURCES.md` | Not started |
| FEED-02 | Sector performance | Sector index levels and 1-week, 1-month, 3-month changes (Nifty IT, Bank, Pharma, Auto, FMCG, Metal, Realty, Energy); "Which sectors are doing well this month?" answers with them (A8) | `graph/tool_catalogue.py` (new tool), `tools/data/nse_scraper.py` or vendor | Not started |
| FEED-03 | Fund metrics beyond returns | TER, AUM, standard deviation, rolling 3-year returns in `mf_metrics`; MF answers rank by more than return | `tools/data/mf_data.py`, `tools/data/amfi.py`, migration, `scripts/refresh_mf.py` | Not started |
| FEED-04 | News that is relevant | Source allow-list, 30-day window, dedupe by title, language tag (translate or drop non-English), no social posts; sentiment label shown only from a model scorer with a confidence, else omitted | `tools/data/news_fetcher.py`, `tools/analysis/sentiment.py` | Not started |

### Platform

| ID | Requirement | Acceptance criteria | Files | Status |
| --- | --- | --- | --- | --- |
| PLAT-03 | Run on more than one process | Orchestrator cache and session state moved to Postgres or Redis; 2 replicas in `docker-compose.yml`; 20-user load run under 1.5x; 2-hour soak flat | `services/chat.py`, `agents/graph_orchestrator.py`, `graph/checkpointer.py`, `docker-compose.yml`, `loadtests/` | Not started |
| BIZ-01 | A free tier a research thread does not exhaust | Daily token budget replaces the 5-message count; clarifications and context-only follow-ups (CONV-01) are free; remaining budget shown in the composer; admin and BYO exempt | `agents/graph_orchestrator.py::_trial_quota_exceeded`, `services/settings.py`, `ui` / `frontend/` | Not started |
| BIZ-02 | Onboarding for the pilot | First-run flow: pick 3 to 5 watchlist stocks, optional holdings import, digest opt-in; three example questions tied to what they picked | `frontend/`, `services/` | Not started |

## Phase 4 — v3.1 Scale

Phase 4 opens sign-ups, adds a paid tier that covers its cost, and keeps quality from drifting as traffic grows. It starts only after the Phase 3 pilot gate passes; scope will be re-planned from pilot data.

**Gate:** paid tier live with at least one completed payment end to end in staging; live wrong-claim rate under 1 per 100 for four consecutive weeks; cost per active user within the `docs/CAPACITY.md` budget.

| ID | Requirement | Acceptance criteria | Files | Status |
| --- | --- | --- | --- | --- |
| BIZ-03 | Paid tier | Plans and limits (reports, digests, alert count, token budget) as `utils/flags.py`-style config; a payment provider integration; receipts; upgrade and cancel flows; audit entries; `LEGAL.md` and a pricing page | `services/` (new billing), migration, `web/`, `frontend/` | Not started |
| BIZ-04 | Open sign-ups | Invite gate removable by a flag; abuse limits (per-IP sign-up, email verification already in place) re-checked under load; status page | `utils/auth.py`, `utils/flags.py`, `web/` | Not started |
| EVAL-04 | Live-trace eval loop | Every week: sample thumbs-down and flagged turns, label, add the useful ones to EVAL-01 and the judge set; dashboard of eval scores over time | `scripts/feedback_triage.py`, `evals/` | Not started |
| EVAL-05 | Prompt A/B tests | A flag splits users between two prompt versions; compares thumbs-down, note rate and length; a winner needs EVAL-01 green | `utils/flags.py`, `prompts/`, `utils/tracing.py` | Not started |
| PLAT-04 | Production cost and tracing at scale | Trace sampling (all errors and thumbs-down, 10% of the rest); per-user and per-intent cost dashboard; paid Groq tier or provider mix chosen from measured cost per turn | `utils/tracing.py`, `utils/metrics.py`, `config.MODEL_ROUTING` | Not started |
| PLAT-05 | Database growth | Paid Neon tier sized from `docs/CAPACITY.md`; retention reviewed for chat history and traces; restore drill re-run on the new tier | `utils/retention.py`, `scripts/restore_drill.py`, `docs/PRIVACY.md` | Not started |

## Engineering contracts for v3 work

Every requirement above also follows the existing production-readiness contracts in `CLAUDE.md` (logging, errors, config, layering, migrations, tenant isolation, advice boundary, prompts, retention). v3 adds these:

1. **One requirement, one branch.** Branch names carry the id (`corr-01-dividend-field`); the PR lists the acceptance criteria as a checklist and links this PRD.
2. **Every fix starts with a failing case.** A unit test for code bugs, and an EVAL-01 case for anything a user reads. The 30 review probes are the first cases.
3. **No answer change without EVAL-01.** A prompt, node or formatter change must leave EVAL-01 at 0 wrong figures and 0 breaches; a prompt change re-records its fixtures (`evals/runner.py --record`, then `make eval-baseline`).
4. **Claims are checked in code where possible.** A new kind of comparative or numeric statement the model may make needs a rule in `graph/claims.py` or `graph/grounding.py`, not a prompt instruction alone.
5. **Data fields need plausibility tests.** Any new field read from a provider gets a range check in `tools/data/contracts.py` and a golden value test.
6. **Notes name claims.** A new caveat must name the claim or figure; generic notes fail a test.
7. **Writes go through the confirm card.** New write tools are `side_effect=True, cacheable=False`, owned by an intent, and confirmed (CONV-03).
8. **Latency budgets per node.** A change that adds an LLM call or a serial tool wave states its cost in the PR and keeps `turns` p95 within the phase target.
9. **Logs as before.** Each shipped item gets a changelog entry in `CHANGELOG_ARIA.md` (IST timestamp, branch); boundary or persistence changes (SURF-01, FEED-01, PLAT-03, BIZ-03) get an `ARCHITECTURE.md` decision entry.
10. **Live verification.** Each phase ends with a live smoke run written up in `docs/smoke/`, using the throwaway database for probes and the real one only with the owner's approval.

## Dependencies, risks and open questions

The two biggest risks are the data licence (cost and terms decide FEED-01 and the stock pages) and the front-end rewrite (largest item, easy to over-scope); both need owner decisions before Phase 3 starts.

### Dependencies

| Item | Depends on | Owner action |
| --- | --- | --- |
| Phase 2 gate (p95 under 12 s) | PERF-02 database region | Create an Asia-region Neon project and migrate (owner TODO file) |
| CONV-03, CONV-04 | CORR-04 advice classifier (write intents must not be advice) | — |
| SURF-02, SURF-03 | FEED-01, FEED-03, SURF-01 | Choose a data vendor and plan |
| SURF-05 digest | Telegram bot and SMTP already configured | Confirm sender address and opt-in text |
| SURF-07 pilot | BIZ-01, BIZ-02, `docs/PRIVACY.md` update | Invite 20 users |
| BIZ-03 | Pricing decision, payment provider account | Pick provider; GST and invoicing advice |

### Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Claim checker misses phrasing variety | Medium | Wrong claims still ship | Start with the review probes, add a rule per new miss from EVAL-04; model check as second layer |
| Rules pack goes stale | Medium | Wrong tax answers again | Review date per entry; a test fails past the date; Budget day checklist |
| Vendor data costs more than the budget | Medium | FEED-01 slips | Vendor for fundamentals only; keep NSE archives for prices |
| Front-end rewrite runs long | High | Phase 3 slips | Ship pages one at a time beside Gradio; parity checklist; cut admin to last |
| Groq free tier or model changes | Medium | Latency or empty replies | Model health probe, Gemini route, EVAL-01 on the fallback route |
| Advice regulation tightens (SEBI) | Low | Features must be cut | Education-only stays a hard rule; legal review before paid tier |
| Pilot too small to learn from | Medium | Retention gate is noisy | Pair the 40% target with qualitative interviews; extend to 50 users if mixed |

### Open questions for the owner

- [ ] Which data vendor and budget for FEED-01?
- [ ] React or HTMX for SURF-01?
- [ ] Should alerts keep a fast path without the confirm card for power users?
- [ ] Free-tier daily token budget for BIZ-01, and paid-tier price points for BIZ-03?
- [ ] Telegram, email, or both for the digest by default?
- [ ] Who are the 20 pilot users, and when does the pilot start?
- [ ] Should this PRD replace the production-readiness PRD link in `CLAUDE.md`, with an offline snapshot in `docs/`?
