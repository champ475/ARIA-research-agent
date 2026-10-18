# ARIA consolidated issues report, 2026-10-18

Sources: `docs/smoke/2026-10-17-live-smoke.md` (with its live re-run), `docs/smoke/2026-10-18-ui-redesign-smoke.md`, and a fresh pull of the LangSmith project `aria-research-agent-local` (1,061 spans, 75 chat turns, 2026-10-17 06:03 to 2026-10-18 05:22 UTC, read-only). Code paths were read to confirm each root cause. Nothing was changed except this file.

Scope note: `main` is at the merge of `smoke-test-fixes` (PR #36). The redesign is on `ui-redesign-night-ledger` (draft PR #37), so U-items are fixed there, not on `main`.

## 1. What the traces say (the real root causes)

| Metric | Pre-fix (10/17 before 19:00 UTC) | Post-fix |
|---|---|---|
| Analyzer empty replies | 10 of 27 | **0 of 42** |
| Judge calls that flagged an issue | 15 of 22 | **14 of 34 (41%)** |
| ...of those, `missing_citation` only | 10 | **14 (all)** |
| Hard gate fired | 8 interrupts | 0 |
| Mean LLM calls / turn, all turns | | 3.2 |
| Mean turn time | | 11.8 s (median about 9.4, p95 25.9) |

P0-1 (empty analyzer) is verifiably gone. The gate is gone. But the judge is still noisy underneath, and the traces show why several UI-smoke failures happened.

### T-1 The judge flags the user's question as an uncited claim (explains B-4, and wasted calls)
Every post-fix judge flag is `{"claim": "<the user's objective verbatim>", "issue_type": "missing_citation", "evidence_ids": [one id]}`, e.g. claim "Analyse TCS weekly", "Which stock should I buy?", "Generate a full research report on TCS". Prompt `verification.system.v2.md` lists five conditions and none is about citations, yet `missing_citation` is in the output enum, so the model uses it as a catch-all when it is unsure. It does not look at the answer (there is none yet); it is judging the objective.
- Effect 1: the "Could not fully confirm: <your question>" note (B-4) is this issue rendered. It is a judge artefact, not a real gap.
- Effect 2: the flag sends the graph into Reflection and another judge pass (+2 to 3 LLM calls, about 3 s), and sometimes extra tools. "Which stock should I buy?" ran Reflection twice, added `peer_comparison`, judged again, got the same flag, and ended with a soft note after 34 s.
- Why calibration looked fine (precision 0.96): the replay set is golden cases, not live evidence. Live flag rate is 41%.
- Fix: remove `missing_citation` from the enum (or make the claim a sentence from the conclusions, never the objective), add "never use the objective as a claim" to the prompt, and reject in code any issue whose claim equals the objective. Re-record `evals/fixtures/judge.jsonl`, re-run `judge_calibration.py`, and add live-trace flag rate as a metric.
- Files: `prompts/verification.system.v2.md`, `graph/nodes/verification.py`.

### T-2 Reflection asks for data that was already fetched
"Analyse TCS weekly": `fetch_ohlcv` had run, and Reflection still reported `missing: weekly price data, action: fetch_ohlcv`. Reflection cannot see which tools already ran, or it is driven by the T-1 flag. 13 reflection passes asked for more tasks (0 to 4 each). Fix: pass the done-task list into the prompt and drop requests for tools already completed. Files: `graph/nodes/reflection.py`.

### T-3 Company carry-over runs on every company-less message (root cause of B-1)
The analyzer LLM is right: for "What does a high P/E ratio mean?" and "Which stock should I buy?" it returned `companies: []`, `complexity: simple`. Then `graph/nodes/query_analyzer.py:210-215` calls `history_carry(history)` whenever the message names no company, with no check that the message points at one. It injects TCS (or INFY), and `min_complexity` (`graph/data_questions.py:55`) raises complexity to moderate "because it names TCS". The planner then gets `Entities: TCS`, returns `tools: []` for the concept question, and Communication says "no conclusions to report" (the "couldn't find enough data" answer). For "Which stock should I buy?" it plans 6 tools for INFY and researches a stock the user never mentioned.
- Fix: carry a company over only when `_COMPANY_POINTER` matches (`graph/entity_resolution.py:448`, the same test `lacks_company` already uses) or the intent is a follow-up. Also make a planner result of zero tools on a chat intent fall back to the direct (concept) path instead of "no conclusions". Add trace-derived regression cases to `tests/graph` and the golden set.

### T-4 Timeframe word leaks into the objective (B-2)
`services/chat.py:129 with_timeframe` appends "weekly" whenever `classify()` says equity, with no company check. The analyzer then copies it into `objective` ("What does a high P/E ratio mean? weekly"), and the planner sees it. Fix: append only when a company is resolved, and pass the timeframe as state (also fixes B-3, the chart title ignoring it), never as query text.

### T-5 Input glue is still happening (`How is HDFC doing?the bank one`, `aGenerate a full research report on TCS`)
Traces show these exact strings reaching the graph (10-17 20:45, 10-18 05:22), the second one on the redesign build. Smoke notes call H-1/H-2 harness artefacts and say the leftover-text cause was "understood" (Gradio drops a server-side `value=""`). The stray "a" case is a new variant that a browser-side clear does not obviously explain. Treat as open until reproduced with real typing. Files: `ui/chat.py` `APP_JS`.

### T-6 Latency is LLM synthesis and serial tool waves, not the graph
- Tool time per call: `fetch_financials` p50 4.1 s (max 8.5), `fetch_info` 2.3 s, `fetch_ohlcv` max 8.2, `fetch_news` max 5.7. Waves run in parallel but a second wave (`compute_*`, 2.2 s) waits on the first, so tools cost about 12 to 15 s of a research turn.
- Synthesis: the 31 s call (Analyse TCS weekly) had a 4.4k-character prompt and a 2.7k-character answer, about 700 tokens. That rate is far below Groq's normal speed, so the time is hidden reasoning tokens, a 429 retry or provider fallback inside the call, or queueing. The spans do not say which.
- Prompts carry junk: raw yfinance `info` (address, phone, fax, `longBusinessSummary`) is inside the evidence sent to the judge and reflection. "Which stock should I buy?" built **91 conclusions from 7 evidence items**.
- Observability gap: every LLM span has `tokens=0`, `model=""`, no finish reason. Reasoning tokens, model chosen, fallback hops and retries are invisible in LangSmith, so P-2 cannot be fully diagnosed from traces. The `turns` table has metering (LLM-11); the spans should carry the same fields.
- Fixes: strip non-analytic fields from `quote_info` before summarising; set `ls_model_name`, usage metadata and a `fallback_from` tag on every LLM span; cap Communication `reasoning_effort` for non-report intents; look at the slow synthesis calls in `turns` for model and token counts.

### T-7 Smaller trace findings
- Gate interrupts show as `status=error` (8 `GraphInterrupt` on `VerificationGate`). They are not errors; it pollutes error dashboards. Mark the run successful or filter.
- Reflection is correctly skipped on 39 of 61 passes (cheap); Planner and Analyzer are 0.6 to 0.8 s each (fine).
- No exceptions, no 429 and no timeouts in 1,061 spans. Provider reliability is not the problem.

## 2. Consolidated issue list, by phase

Status key: OPEN, FIXED-main (merged, verified live), FIXED-branch (on the redesign branch, PR #37), UNTESTED.

### Phase A: wrong answers for ordinary questions (do first)
| ID | Issue | Root cause | Status |
|---|---|---|---|
| A1 | Concept question after a company chat: "couldn't find enough data" (B-1) | T-3 carry-over without a pointer | OPEN |
| A2 | "Which stock should I buy?" researches INFY (B-1) | T-3 | OPEN |
| A3 | "weekly" echoed in answers, wrong chart title (B-2, B-3) | T-4 | OPEN |
| A4 | "Could not fully confirm: <question>" on good answers (B-4) | T-1 judge artefact | OPEN |
| A5 | Fabricated illustrative numbers flagged "Unverified" (B-5) | grounding check runs on concept answers | OPEN |
| A6 | Wrong fund claim "low NAV means small AUM" (C-2) | no prompt rule / check | OPEN |
| A7 | Alerts created without confirmation (B-6, P1-13) | write policy needs only a verb; the 20%/3x price guard exists but percentage alerts are unchecked | OPEN (design call) |
| A8 | "Which sectors are doing well" answers with revenue growth (PARTIAL) | no sector-performance tool | OPEN |

### Phase B: user-facing dead ends and reliability
| ID | Issue | Status |
|---|---|---|
| B1 | PDF link returns 503 when WeasyPrint is missing (E-1); hide or explain the link | OPEN |
| B2 | Database unreachable: Send does nothing, no message (E-2) | OPEN |
| B3 | Stale chat view after a network drop (E-3) | OPEN, not reproduced |
| B4 | Input glue "xxxThe next question" / "aGenerate..." (T-5) | OPEN |
| B5 | Idle Neon pool timeout | FIXED-main (idle 180 s, acquire 20 s, retry); re-verify after a long idle |
| B6 | Non-admin trial limit, 375 px live layout, PDF/MD click, rename/export, thumbs-down reason, concurrent sends | UNTESTED |

### Phase C: speed and cost
| ID | Issue | Evidence | Status |
|---|---|---|---|
| C1 | Judge + Reflection loop on a false flag (T-1, T-2) | +2 to 3 LLM calls on 41% of judged turns | OPEN |
| C2 | Bloated evidence prompts, 91 conclusions from 7 items (T-6) | | OPEN |
| C3 | Synthesis 12 to 31 s with a small prompt (T-6) | needs token/model on the span | OPEN |
| C4 | Persist 1.1 to 2.3 s per turn (P-1) | single transaction already; cross-region RTT to `us-east-1` | OPEN (region or defer write) |
| C5 | Serial tool waves, `fetch_financials` 4 s | | OPEN |
| C6 | Span metadata missing; `GraphInterrupt` logged as error (T-6, T-7) | | OPEN |

### Phase D: answer quality and content
| ID | Issue | Status |
|---|---|---|
| D1 | "₹₹398000.00Cr" doubled sign (C-1) | OPEN |
| D2 | RSI four decimals on Watchlist (C-3) | OPEN |
| D3 | Indian grouping and 2-decimal crore figures (C-4) | OPEN |
| D4 | EMMVEE listed as IT in the screener (C-5, P2-2) | OPEN |
| D5 | Screener shows no row count, sort order or universe size | OPEN |
| D6 | Empty-portfolio reply carries a market-closed line and report footer (P2-5) | OPEN |
| D7 | Planning never asks retirement age; saved profile may be stale (P2-6) | OPEN |
| D8 | Follow-up chips generic for non-equity intents (B-7) | OPEN |
| D9 | Reframe answer for "which stock should I buy" still long (P2-4) | partly fixed (prompt v2) |

### Phase E: tests so these cannot return
- Seeded user with 20+ chats and long answers; assert sidebar visible and nothing overlaps `#app-bar` (U-1, U-2).
- Feedback panel open/close, Number field initial state, 1536x730 viewport, database-unreachable journey (U-3, U-4, U-6, E-2).
- Trace-derived golden cases: the five failing prompts above, plus a judge-flag-rate metric over live traces (target under 10%).
- Guardrail gaps found earlier are fixed on `main`; keep them in `tests/`.

### Already fixed and verified (no action)
Analyzer empty replies (0 of 42 post-fix), gate over-firing, fabricated market verdict (index tool), MF names, company-less "which company?" ask, Infosys currency ratios, NaN cache write, admin trial badge, guarantee guardrail ("Guarantee me 20%" now refused with 0 LLM calls), message-box clear, per-turn persist cost cut.

## 3. Suggested order
1. T-3 and T-4 (A1 to A3): small, deterministic, no prompt re-record.
2. T-1 and T-2 (A4, C1): prompt change, so re-record the judge fixtures and calibration; this also removes about 40% of reflection loops.
3. B1, B2, B4 (dead ends).
4. C2, C3, C6: trim evidence, add span metadata, then re-trace the slow prompts before tuning anything else.
5. Phase D and E.

## 4. Caveats
- LangSmith stores only 1.5k characters of each input/output here (my pull truncated them), so claims about judge output rest on the first 2.5k characters, which held the full JSON.
- The pre/post split is by clock time (19:00 UTC on 10-17), approximating the day the fixes were deployed.
- Latency causes in T-6 are inferred from sizes and timings; the spans lack token and model data to prove them.
