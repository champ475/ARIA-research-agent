# ARIA UI redesign smoke test, 2026-10-18

Branch `ui-redesign-night-ledger` (draft PR #37), from commit `20f83be` up to `e7e7e31`. Two parts:

1. The offline browser suite (`tests/e2e`) on a throwaway Postgres (`aria-test-pg`) with the fake LLM and fake market data.
2. A live run on `http://localhost:7860` against the real Neon database and real LLM keys from `.env`, driven through Claude in Chrome as the admin account. Background jobs were off (`RUN_JOBS_IN_APP=0`), so no alerts or Telegram messages were sent.

The goal was to check that the "Night Ledger" redesign landed as intended and that it broke nothing that worked after the 2026-10-17/18 smoke fixes (`docs/smoke/2026-10-17-live-smoke.md`). This file also records everything else seen on the way, for later product work.

## Headline

| | |
|---|---|
| Offline browser tests | **29 / 29 pass** (7 accessibility, 4 mobile, 18 journeys) after one fix |
| Unit and integration tests | 2,509 passed, 1 skipped (before the live fixes); 292 UI-related re-run after them, all pass |
| Live chat turns | 14 sent; 11 passed (one is the intended refusal), 1 partial (PDF download), 2 failed (both research-graph behaviour, not UI) |
| UI bugs found live, caused by the redesign | **7**, all fixed in `e7e7e31` |
| Accessibility failure found offline, caused by the redesign | 1, fixed in `20f83be` |
| Errors in the server log | 1 (`[session] could not resolve the session cookie`, during a network outage) |
| Turn latency (server, `turn end ms`) | median about 11.8 s; fastest 2.1 s (refusal); slowest 55.7 s (`Analyse TCS`) |
| Fixed cost per turn | 1.1 to 2.3 s spent saving the turn to Neon (`persist_ms`) |

## Results: live chat turns

Times come from the server's `[respond] turn end` line: total / answer (graph + LLM) / persist. The orchestrator time was 0 ms on every turn except the fund comparison (15 ms). Suggestion time was logged as 0 ms on every turn.

| # | Prompt (chat) | Verdict | Total / answer / persist (ms) | What happened |
|---|---|---|---|---|
| 1 | How is Reliance doing? (new chat) | PASS | 27,562 / 23,078 / 1,375 | Snapshot table, Sources (3), research line and 6 progress steps shown, markets-closed banner. No chart for this wording. |
| 2 | what about its debt? | PASS | 11,391 / 10,157 / 1,203 | Kept Reliance; debt table and cited D/E calculation. Shows "₹₹398000.00Cr" (see C-1). |
| 3 | How is HDFC doing? | PASS | 4,905 / 3,437 / 1,438 | "Which HDFC do you mean?" with Bank / Life / AMC. Box emptied and refocused afterwards. |
| 3b | the bank one | PASS | 18,421 / 16,796 / 1,610 | Resolved to HDFC Bank, full snapshot. |
| 4 | Analyse TCS (timeframe 1W) | PASS, slow | 55,687 / 53,406 / 2,250 | Chart shown, topic chips ("TCS technicals", …). Answer says "Weekly Snapshot", chart title says "1D" (see B-3). Slowest turn of the run. |
| 5 | What does a high P/E ratio mean? (same chat, 1W still selected) | **FAIL** | 6,765 / 5,484 / 1,187 | "I couldn't find enough data to answer: What does a high P/E ratio mean? weekly". The timeframe word was appended to a question with no company (B-2). |
| 5b | same question, 1D, same chat | **FAIL** | 5,641 / 4,437 / 1,141 | Same "couldn't find enough data". Earlier company context pulls a concept question into research (B-1). |
| 5c | same question, fresh chat | PASS | 15,328 / 11,469 / 1,187 | Proper explanation. Grounding flagged 4 figures as unsupported (`₹500, ₹10, 500, 50`), shown as an "Unverified figures" note (B-5). |
| 6 | IT stocks with PE under 25 and ROE above 15% | PASS | 12,125 / 10,938 / 1,172 | 8-row table, Sources, screener chips; CSV export returns 200 `text/csv`. EMMVEE listed as IT (known open item). |
| 7 | Compare Parag Parikh Flexi Cap and HDFC Flexi Cap | PASS | 9,078 / 7,938 / 1,125 | Returns and volatility table. Wrong claim that the lower NAV reflects a smaller asset base (C-2). |
| 8 | Alert me if INFY falls 5% | PASS | 7,562 / 6,407 / 1,140 | Alert #80 created at once (no confirmation step). Then paused and deleted from the Alerts tab. |
| 9 | Which stock should I buy? | PASS (boundary), see note | 36,610 / 35,250 / 1,328 | No pick, footer present, but it researched Infosys (from the alert turn) instead of the general checklist; ends "Could not fully confirm: Which stock should I buy?" (B-1, B-4). |
| 10 | Guarantee me 20% returns every month | PASS | 2,062 / 781 / 1,203 | `[guardrails] refused (guarantee)`. This failed on 2026-10-17 and now holds. |
| 11 | Generate a full research report on TCS | PARTIAL | 24,937 / 23,703 / 1,187 | Report, Sources (5), chart, Download PDF / Markdown pills. Markdown 200 (5.5 KB); **PDF 503** (E-1). Ends "Could not fully confirm" (B-4). |

Turn 4 also had one failed start: the Send click fired in the same instant as the timeframe change and was dropped. That was harness timing; a person can't do both in one moment.

## Results: UI and other features (live)

| Area | Check | Verdict |
|---|---|---|
| Shell | Sticky app bar, wordmark, plan pill ("Admin · no daily message limit"), icon tabs with underline, Admin tab for admin | PASS |
| Theme | Toggle to light: `<html>` and `<body>` switch, `aria-pressed` updates, stored in `localStorage`, `aria_theme` cookie written, survives reload | PASS |
| Research line | Blue line along the bar while a turn runs | PASS |
| Composer | Box empties and keeps focus after a turn; chips fill the box | PASS |
| Feedback | Thumbs-down opens the panel with 4 reasons and a details box; Skip closes it | PASS after fix (U-4) |
| Sidebar | New chat, active marker, open an old chat (all 7 turns load from the database), rename, export ("Download this chat"), full-text search, Clear chat | PASS after fix (U-1) |
| Watchlist | Add TCS, remove TCS, gain/loss chips on live prices, mono figures | PASS |
| Alerts | Table, Active pill; Pause gives a muted "Paused" pill; Delete removes the row | PASS after fix (U-6) |
| Portfolio | Empty state, Add-a-holding panel, accordions | PASS (copy fixed, U-7) |
| Settings | Three sections in panels, status line for the admin account | PASS |
| Admin | Find form, feature-flags table | PASS after fix (U-5) |
| Downloads | Screener CSV 200; report Markdown 200; report PDF 503 | PARTIAL (E-1) |

## Results: offline browser suite (`tests/e2e`, fake LLM and data)

| File | Result | Time |
|---|---|---|
| `test_a11y.py` | 7 / 7 after the fix below (first run: 2 failed) | 59 s |
| `test_mobile.py` | 4 / 4 (no sideways scroll at 375 / 768 / 1280 px, phone history drawer) | 85 s |
| `test_journeys.py` | 18 / 18 | 155 s |

First run failure: axe `scrollable-region-focusable (serious)` on the chat box, light and dark. The taller welcome note made the conversation scroll, and keyboard users could not focus it. Fixed by giving `.bubble-wrap[role=log]` `tabindex="0"` in `APP_JS` (`20f83be`).

These tests pass with only one or two chats per user, so they could not catch U-1 (long chat list) or U-2 (scrolling under the bar). See "Test gaps".

## Findings

IDs: **U** = UI bug from the redesign (fixed), **E** = error, **P** = performance, **B** = behaviour of the research graph, **C** = content quality, **H** = harness or environment.

### UI bugs from the redesign (all fixed in `e7e7e31` unless noted)

- **U-1 Sidebar list hidden with many chats.** With 20 chats the sidebar's fixed height plus Gradio's `flex-wrap: wrap` pushed the whole panel into a second, clipped column; the sidebar looked empty. Fix: `flex-wrap: nowrap` and no shrinking on `.chat-sidebar`.
- **U-2 Chat icons over the sticky bar.** The chatbot's share / clear / copy strip has `z-index: 20`, the same as the app bar, and wins by DOM order when the page scrolls. Fix: bar 40, tab strip 35.
- **U-3 Welcome heading clipped.** The note used `min-height: 100%` plus top padding without `border-box`, so it was always taller than the chat box and Gradio scrolled past the heading. Fix: `box-sizing: border-box`, and a tighter note on windows under 820 px tall (a 1536×730 laptop at 125 % zoom).
- **U-4 Empty frame after Skip.** Gradio keeps the feedback group's wrapper when hidden; the new border and padding drew an empty rounded box above the chips. Fix: `#feedback-chat:has(.form.hidden) { display: none }`.
- **U-5 Table borders on Admin / Portfolio.** Gradio's prose styles draw borders on `tr`. Fix: reset `tr` borders inside prose tables.
- **U-6 "Alert #" red on load.** Gradio shows an empty Number as 0, below `minimum=1`, and marks it invalid with the (now red) error border before the user types. Fix: no browser minimum on "Alert #" and "Lot #"; the handlers already refuse 0 and unknown ids.
- **U-7 Stale emoji tab names in messages.** "Add holdings in the 📊 Portfolio tab" (`tools/portfolio.py`) and "Update it in ⚙️ Settings" (`agents/llm_client.py`). Fixed to plain names.
- **U-8 Zero change shown as a gain.** "▲ +0.00%" rendered as a green chip. Fix: exact 0.00% is a neutral chip (`.chg.flat`).
- **U-9 Accessibility, scrollable chat not focusable.** See the offline suite above (`20f83be`).

### Errors

- **E-1 PDF download returns 503 on this machine.** `utils/report_files` logs `PDF render failed, Markdown only: OSError`; `/download/report/19.pdf` returns the intended "PDF rendering is unavailable" 503. Cause: WeasyPrint's native GTK libraries are not installed on Windows. The Docker image has them. Not a regression (the real PDF click was never exercised live before). **Product follow-up:** hide or disable the PDF link when rendering is unavailable, or say why next to it, instead of offering a link that 503s.
- **E-2 Silent failure when the database is unreachable.** A DNS outage (`failed to resolve host '…neon.tech': getaddrinfo failed`) made the session lookup fail (`[session] could not resolve the session cookie`, the only ERROR in the log). In the browser, clicking Send did nothing: no message, no error, the text stayed in the box. **Follow-up:** when the request can't be authenticated because the database is down, show "ARIA can't reach its database right now, try again in a moment" (and an ops alert), not silence.
- **E-3 Stale chat view after the outage.** In the same tab, after the network came back, two finished answers were missing from the chat view while the database had them; opening the chat from the sidebar showed all of them. Not reproduced after a reload. Likely Gradio's client lost an update while the connection dropped. **Follow-up:** worth a reconnect test (drop the network mid-turn) and, if it reproduces, reload the active chat from the database on reconnect.

### Performance and latency

- **P-1 Persisting a turn costs 1.1 to 2.3 s, every turn** (`persist_ms`; mean 1.32 s over 14 turns). That is a fixed cost on even the 2 s refusal (persist was 1.2 s of 2.06 s). It is the remote round trips to Neon (`us-east-1`) from India. **Ideas:** write the turn after the answer is shown (it already streams), batch the statements into one round trip, or use a region closer to the users.
- **P-2 Slow research turns.** `Analyse TCS` took 55.7 s (53.4 s in the graph); "Which stock should I buy?" 36.6 s; "How is Reliance doing?" 27.6 s (32 s in the browser). Simple lookups and clarifications took 5 to 12 s. LangSmith tracing was not on for this run, so the split between tools and LLM calls per node is not known. **Next step:** re-run the slow prompts with `ARIA_TRACING=langsmith` and look for sequential tool calls and repeated LLM passes (reflection / verification).
- **P-3 First page load.** The sidebar's chat list and the welcome note appear a moment after the page; on a cold start the first screenshot showed an empty sidebar. Not measured precisely.
- **P-4 Browser overhead.** The new CSS uses `:has()` selectors (research line, feedback panel). No slowdown was visible, but they are re-evaluated on DOM changes during streaming; keep an eye on long answers on slow phones.

| Turn latency (server total) | ms |
|---|---|
| Fastest (refusal) | 2,062 |
| Clarification ("Which HDFC?") | 4,905 |
| Median of 14 | about 11,760 |
| Slowest (`Analyse TCS`) | 55,687 |

### Research-graph behaviour (not UI; for the product backlog)

- **B-1 Earlier topic leaks into later questions.** In a chat about TCS, "What does a high P/E ratio mean?" got "I couldn't find enough data" twice; in a fresh chat it got a good answer. In a chat whose last turn was an INFY alert, "Which stock should I buy?" researched Infosys instead of giving the general checklist (as it did on 2026-10-17 in a fresh chat). The "points at a company" carry-over (ARCHITECTURE.md, 2026-10-18 entry) seems to fire on questions that name no company and use no pronoun.
- **B-2 Timeframe word appended to every equity-classified question.** `services/chat.with_timeframe` adds "weekly" / "monthly" / … whenever the orchestrator classifies the message as equity, including concept questions; the answer then echoes "…mean? weekly". Only append when a company was resolved, and never show the keyword back to the user.
- **B-3 Chart title ignores the timeframe.** With 1W selected the answer was a "Weekly Snapshot" but the chart title said "TCS (TCS.NS) — 1D"; the chart is the same daily series. The timeframe only reaches the LLM, not the chart tool.
- **B-4 "Could not fully confirm: <the whole question>" notes.** The report and the advice answer both end with "Could not fully confirm: Generate a full research report on TCS." Quoting the user's own question reads as if the question was wrong; the note should name the claim that could not be confirmed (as the hard gate does).
- **B-5 Grounding flags numbers the answer made up for illustration.** The P/E explanation used example figures (₹500, ₹10, 500, 50) and the answer carried an "Unverified figures" warning. For concept answers, illustrative numbers should either be skipped by the grounding check or worded as examples.
- **B-6 Alerts are created without confirmation.** "Alert me if INFY falls 5%" created Alert #80 immediately (also noted on 2026-10-17 for an implausible ₹1 level).
- **B-7 Follow-up chips are generic for most intents.** Only equity analysis and the screener produced topic chips; every other turn showed "Analyse a stock / Compare mutual funds / Top gainers today / Set a price alert". `suggestions_ms` was 0 on every turn, so chips may come from a fixed list for those intents.

### Content quality

- **C-1 Doubled rupee sign** in cited calculations: "₹₹398000.00Cr / ₹₹904030.00Cr" (debt-to-equity working). The formatter adds "₹" to a value that already has one.
- **C-2 Wrong claim in the fund comparison:** "Parag Parikh's NAV is much lower, reflecting its smaller asset base." NAV per unit says nothing about fund size. A prompt rule ("don't infer AUM from NAV") or a verification check would catch it.
- **C-3 RSI shown with four decimals** on the Watchlist (74.1368); one decimal is enough.
- **C-4 Large raw numbers** in snapshots: "Market cap ₹1,639,052.95 cr" uses Western grouping and two decimals on crore figures; Indian grouping (₹16,39,053 cr) would read better.
- **C-5 EMMVEE under IT** in the screener (open since 2026-10-17).

### Harness and environment (not product bugs, but they cost time)

- **H-1 Synthetic input is unreliable in this app.** Setting the textarea value from JavaScript and clicking Send sometimes did not start a turn. Real typing plus a real click always worked. Use real input for live smoke tests (as the 2026-10-17 notes also say).
- **H-2 Stale click coordinates.** Two "lost" messages were clicks that landed on a suggestion chip after the page scrolled; always re-measure the box before clicking.
- **H-3 Enter key, unconfirmed.** About 3 in 8 Enter presses sent from the Chrome extension did not submit, while Send clicks always did. The e2e journeys press Enter over 20 times without a miss, so the extension is the likely cause. Not compared against `main` in this run; do that before treating it as a product bug.
- **H-4 Chrome screenshots time out after a reload** of the Gradio page ("renderer may be frozen"); a fresh tab fixes it. Headless Playwright screenshots were reliable.
- **H-5 Memory pressure.** The full e2e run and the preview server were killed once by the session's low-memory reaper; running the e2e files one by one in the foreground worked.
- **H-6 Network outage.** A DNS failure for about a minute caused E-2 and E-3 and disconnected the Chrome extension.

## Test gaps found

- The e2e tests create a new user with one or two chats, so the sidebar with a long list (U-1) and scrolling under the sticky bar (U-2) are never exercised. Add a seeded user with 20+ chats and a long answer, and assert the first chat button is inside the sidebar's visible box and nothing overlaps `#app-bar`.
- No test opens and closes the feedback panel (U-4) or checks a Number field's initial state (U-6).
- No test runs at a short viewport (U-3); add 1536×730 to `test_mobile.py` sizes.
- No test covers "database unreachable" from the browser (E-2).

## Not tested

- Log out and the logged-out login / sign-up pages in the live app (logging out would have ended the owner's session). Both were checked in the offline e2e suite and in headless screenshots.
- Telegram connect, BYO key save, admin user lookup and flag changes, CSV portfolio import, the yes/no confirmation card (no turn hit the hard gate), planning and portfolio questions in chat.
- Phone widths in the live app (covered offline by `test_mobile.py` and headless screenshots).

## State left behind (Neon, admin account)

- New chats: "UI smoke 2026-10-18" (renamed; 7 turns), "What does a high P/E ratio mean?" (7 turns: concept, screener, MF, alert, advice, guarantee, report), plus earlier test chats from 2026-10-17.
- Alert #80 created, paused and deleted. Watchlist: TCS added and removed (back to RELIANCE, HDFCBANK, ITC, ATHERENERG).
- One thumbs-down feedback attempt was skipped (no reason sent); one thumbs-down click may be stored as a dislike on the report answer.
- Theme set to light in this Chrome profile (toggle back with the sun / moon button).

## Suggested order for follow-up work

1. E-2 (silent failure when the database is down) and E-1 (PDF link that 503s): user-facing dead ends.
2. B-1 and B-2 (context leak and timeframe word): they make correct questions fail.
3. P-1 (persist cost) and P-2 (trace the slow turns).
4. B-4, B-5, C-1, C-2: answer quality.
5. The test gaps above, so U-1 to U-6 can't come back.
6. B-3, B-6, B-7, C-3, C-4, C-5, H-3.
