# Cost model and capacity plan (OPS-05)

What a turn costs, how many daily active users (DAU) each free tier carries, and what to buy when it runs out. The model is `docs/capacity_model.xlsx` (formulas over an Inputs sheet, edit a number and the answers move); `scripts/capacity_model.py` writes it and holds the same arithmetic in Python, which `tests/test_capacity_model.py` pins.

## The answer

Cost on the operator's trial keys, with the synthesis model's list price as an upper bound (the fast nodes use a cheaper model):

| Kind | LLM calls | Tokens in | Tokens out | Cost per turn |
|---|---|---|---|---|
| equity | 4.5 | 3,309 | 700 | ₹0.08 |
| mf | 4.5 | 1,297 | 450 | ₹0.04 |
| screener | 4.5 | 1,754 | 350 | ₹0.04 |
| alert | 4.6 | 1,479 | 200 | ₹0.03 |
| report | 4.5 | 2,329 | 1,500 | ₹0.11 |
| **weighted by the mix** | 4.5 | 2,620 | 618 | **₹0.07** |

A user who spends the free trial's five turns a day costs the operator about ₹0.34 a day. A BYO-key user costs nothing in LLM spend.

The first three limits to be hit, soonest first, at five turns per user per day:

| # | Limit | DAU at which it is reached | What lifts it |
|---|---|---|---|
| 1 | Database size (512 MB) lasting 6 months | 85 | A paid Neon plan (a few GB), or archive old chats; retention already caps traces at 30 days |
| 2 | LLM requests per day on the trial keys (1 key(s) x 14,400) | 639 | More trial keys (up to 5 rotate), then a paid Groq tier; BYO-key users cost nothing |
| 3 | App CPU: 1 process(es) at 2.4 turns/s, 25% of turns in the peak hour | 6,912 | More replicas (compose --scale app=N), one more small VM each |

A trial user's 60,000-token daily cap is about 19 turns at 3,237 tokens a turn, so the per-user cap never binds before the five-message limit does.

**What this says to do.** Storage is the first wall, and it is a slow one: at 85 DAU the free database lasts six months, at 400 about five weeks. Upgrade the database before inviting more than a few dozen regular users, or shorten the 24-month chat retention (`utils/retention.py`). The LLM request budget is next: with one key about 640 people a day. Five rotated keys carry about 3,200 (check the per-key limit first, below). CPU comes last, and adding a replica doubles it.

## Where each number comes from

Every input is marked in the workbook as **measured**, **assumed** or **verify**; the cells that are not measured are shaded.

| Input | Value | Status | Source |
|---|---|---|---|
| LLM calls per turn, tokens in | per kind, above | measured | `turns` on the load run (`docs/load/2026-10-17-local.md`): the graph and the prompts are real, the model is the fake one |
| Tokens out | per kind, above | **assumed** | typical answer lengths; `config.TOKENS_*` are the caps. Replace with `turns.tokens_out` after the beta |
| Turns per user per day | 5 | **assumed** | there are no real users yet; this is the free trial's daily limit |
| Peak-hour share of a day's turns | 25% | **assumed** | the NSE session |
| Turns per second one process sustains | 2.4 | measured | the clean load run: the process used about 90% of one core (a laptop, with the load generator and Postgres on the same machine) |
| Bytes stored per turn | about 7 KB | measured, answer length assumed | turns row 1,475 B; chat rows from the load database; checkpoints are 0 since ARCH-04; answers taken as 3 KB |
| LLM requests per key per day | 14,400 | **verify** | the figure `CLAUDE.md` states for the old default model. Check Groq's current limit for the gpt-oss models before relying on it: this one moves limit 2 |
| Database limit | 512 MB | **verify** | `DB_SIZE_LIMIT_MB`; the Neon free tier |
| Model price, USD to INR | $0.15 / $0.60 per million, ₹88 | **verify** | `config.MODEL_PRICES_USD_PER_M`, `USD_INR` |

## Keeping it true

This model is only worth what its inputs are. After two weeks of beta, and then monthly:

1. `python scripts/capacity_model.py --from-db --markdown` reads `DATABASE_URL` (read-only) and replaces the calls, tokens in and out, turns per user per day and the bytes per turn with what the `turns` and `chat_history` tables hold, then rewrites the workbook and prints the two tables above.
2. Paste the tables here, and check the limit lines that say **verify**.
3. The live numbers behind it are on the OBS-03 dashboard: tokens and ₹ per turn, database size, the `aria_gates_total` and job heartbeats. `db_size` pages the operator at 80% of `DB_SIZE_LIMIT_MB` (`docs/RUNBOOK.md`).

Not in the model: provider outages, the nightly data refresh's own use of Yahoo (rate-limited long before any DAU limit), and CI minutes, which grow with commits, not users.
