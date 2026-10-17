"""
scripts/capacity_model.py — what ARIA costs per turn and when each free tier breaks (OPS-05).

    python scripts/capacity_model.py                      write docs/capacity_model.xlsx from the built-in inputs
    python scripts/capacity_model.py --from-db            first replace the inputs the `turns` table can measure
                                                          (run it after two weeks of beta, then monthly)
    python scripts/capacity_model.py --markdown           also print the summary table (docs/CAPACITY.md quotes it)

The workbook has two sheets. **Inputs** lists every number with where it came
from (measured / assumed / verify) and can be edited in the sheet. **Model** is
live formulas over the inputs: cost per turn by kind of question, the number of
daily active users (DAU) at which each limit is reached, and the first three
limits to be hit. `limits()` below is the same arithmetic in Python (the tests
pin it); the workbook states the formulas so the owner can change an input and
see the answer move.

`--from-db` reads DATABASE_URL (a read-only SELECT on `turns` and `chat_history`).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1]
KINDS = ["equity", "mf", "screener", "alert", "report"]
MIX = {
    "equity": 0.60,
    "mf": 0.15,
    "screener": 0.10,
    "alert": 0.10,
    "report": 0.05,
}  # the load profile (TEST-06)


@dataclass
class Inputs:
    # LLM use per turn, by kind. Calls and input tokens come from `turns` (measured on the load
    # run, whose prompts are real); output tokens are assumed from the answer budgets in config.py.
    calls_per_turn: dict[str, float]
    tokens_in: dict[str, float]
    tokens_out: dict[str, float]
    price_in_usd_per_m: float  # list price of the synthesis model (config.MODEL_PRICES_USD_PER_M)
    price_out_usd_per_m: float
    usd_inr: float
    turns_per_dau_per_day: float
    peak_hour_share: float  # share of a day's turns that fall in the busiest hour
    # Limits
    llm_keys: int
    llm_requests_per_key_per_day: float
    trial_tokens_per_user_per_day: float
    process_turns_per_second: float  # one app process, saturated (docs/load)
    replicas: int
    db_limit_mb: float
    db_baseline_mb: float
    bytes_per_turn: float  # turns row + chat rows + indexes, kept (see below)
    horizon_months: float  # how long the database must last before it is upgraded


# Measured on 17 Oct 2026 (docs/load/2026-10-17-local.md) unless a note says otherwise.
DEFAULTS = Inputs(
    calls_per_turn={"equity": 4.5, "mf": 4.5, "screener": 4.5, "alert": 4.6, "report": 4.5},
    tokens_in={"equity": 3309, "mf": 1297, "screener": 1754, "alert": 1479, "report": 2329},
    tokens_out={"equity": 700, "mf": 450, "screener": 350, "alert": 200, "report": 1500},
    price_in_usd_per_m=0.15,
    price_out_usd_per_m=0.60,
    usd_inr=88.0,
    turns_per_dau_per_day=5.0,
    peak_hour_share=0.25,
    llm_keys=1,
    llm_requests_per_key_per_day=14_400.0,
    trial_tokens_per_user_per_day=60_000.0,
    process_turns_per_second=2.4,
    replicas=1,
    db_limit_mb=512.0,
    db_baseline_mb=15.0,
    # turns row 1.5 KB; user + assistant chat rows (3 KB answer, 1.4x for the full-text index);
    bytes_per_turn=1475 * 1.5 + (300 + 3000) * 1.4,
    horizon_months=6.0,
)

# Where each input comes from: measured / assumed / verify.
NOTES: dict[str, tuple[str, str]] = {
    "calls_per_turn": ("measured", "turns.llm_calls on the load run (aria_load), fake LLM but real graph"),
    "tokens_in": (
        "measured",
        "turns.tokens_in on the load run: the prompts are real, only the answers are fake",
    ),
    "tokens_out": (
        "assumed",
        "typical answer length for each kind; config.TOKENS_* are the caps. Replace with turns.tokens_out after beta",
    ),
    "price_in_usd_per_m": (
        "verify",
        "config.MODEL_PRICES_USD_PER_M openai/gpt-oss-120b; upper bound (fast nodes use gpt-oss-20b)",
    ),
    "price_out_usd_per_m": ("verify", "same table as price_in_usd_per_m"),
    "usd_inr": ("verify", "config.USD_INR"),
    "turns_per_dau_per_day": (
        "assumed",
        "no real users yet; equals the free trial's 5 messages a day. Measure after beta",
    ),
    "peak_hour_share": ("assumed", "a quarter of a day's turns in the busiest hour (market open)"),
    "llm_keys": ("assumed", "operator's Groq trial keys in .env (up to 5, rotated)"),
    "llm_requests_per_key_per_day": (
        "verify",
        "14,400 is the figure CLAUDE.md states for the old default model; check Groq's current limit for gpt-oss-120b/20b before relying on it",
    ),
    "trial_tokens_per_user_per_day": ("measured", "config.TRIAL_DAILY_TOKENS (the per-user cap, SEC-12)"),
    "process_turns_per_second": (
        "measured",
        "docs/load/2026-10-17-local.md: one process saturated its core at about 2.4 turns/s on a laptop",
    ),
    "replicas": ("assumed", "app processes; `docker compose up --scale app=N`"),
    "db_limit_mb": ("verify", "config.DB_SIZE_LIMIT_MB; Neon free tier is 0.5 GB per PRD"),
    "db_baseline_mb": (
        "measured",
        "migrated database with the screener snapshot and instrument data, before any chats",
    ),
    "bytes_per_turn": (
        "measured",
        "turns row 1,475 B (pg_column_size); chat rows from aria_load; checkpoints are 0 since ARCH-04; answer length assumed 3 KB",
    ),
    "horizon_months": ("assumed", "how long the free database must last before an upgrade"),
}


def cost_per_turn_inr(i: Inputs, kind: str) -> float:
    usd = (i.tokens_in[kind] * i.price_in_usd_per_m + i.tokens_out[kind] * i.price_out_usd_per_m) / 1e6
    return usd * i.usd_inr


def weighted(i: Inputs, series: dict[str, float]) -> float:
    return sum(MIX[k] * series[k] for k in KINDS)


def limits(i: Inputs) -> list[dict[str, Any]]:
    """Each limit, the DAU at which it is reached, and the paid tier that lifts it; soonest first."""
    calls = weighted(i, i.calls_per_turn)
    llm_dau = i.llm_keys * i.llm_requests_per_key_per_day / (i.turns_per_dau_per_day * calls)
    cpu_daily_turns = i.process_turns_per_second * i.replicas * 3600 / i.peak_hour_share
    cpu_dau = cpu_daily_turns / i.turns_per_dau_per_day
    month_turns_per_dau = i.turns_per_dau_per_day * 30
    db_dau = (
        (i.db_limit_mb - i.db_baseline_mb)
        * 1024
        * 1024
        / (i.horizon_months * month_turns_per_dau * i.bytes_per_turn)
    )
    rows = [
        {
            "limit": f"Database size ({i.db_limit_mb:.0f} MB) lasting {i.horizon_months:g} months",
            "dau": db_dau,
            "tier": "A paid Neon plan (a few GB), or archive old chats; retention already caps traces at 30 days",
        },
        {
            "limit": f"LLM requests per day on the trial keys ({i.llm_keys} key(s) x {i.llm_requests_per_key_per_day:,.0f})",
            "dau": llm_dau,
            "tier": "More trial keys (up to 5 rotate), then a paid Groq tier; BYO-key users cost nothing",
        },
        {
            "limit": f"App CPU: {i.replicas} process(es) at {i.process_turns_per_second:g} turns/s, {i.peak_hour_share:.0%} of turns in the peak hour",
            "dau": cpu_dau,
            "tier": "More replicas (compose --scale app=N), one more small VM each",
        },
    ]
    return sorted(rows, key=lambda r: r["dau"])


def trial_token_cap_note(i: Inputs) -> str:
    per_turn = weighted(i, i.tokens_in) + weighted(i, i.tokens_out)
    return f"A trial user's {i.trial_tokens_per_user_per_day:,.0f}-token daily cap is about {i.trial_tokens_per_user_per_day / per_turn:.0f} turns at {per_turn:,.0f} tokens a turn."


def measure_from_db(i: Inputs, database_url: str) -> Inputs:
    """Replace what the `turns` table can tell us (needs at least a few real turns per kind)."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10) as conn:
        rows = conn.execute(
            "SELECT intent, count(*) AS n, avg(llm_calls) AS calls, avg(tokens_in) AS tin, avg(tokens_out) AS tout "
            "FROM turns WHERE intent = ANY(%s) AND status = 'ok' AND created_at > now() - interval '14 days' "
            "GROUP BY intent HAVING count(*) >= 5",
            (KINDS,),
        ).fetchall()
        for r in rows:
            i.calls_per_turn[r["intent"]] = float(r["calls"])
            i.tokens_in[r["intent"]] = float(r["tin"])
            i.tokens_out[r["intent"]] = float(r["tout"])
        per_user = conn.execute(
            "SELECT avg(c) AS turns FROM (SELECT count(*) AS c FROM turns "
            "WHERE created_at > now() - interval '14 days' GROUP BY user_id, created_at::date) d"
        ).fetchone()
        if per_user and per_user["turns"]:
            i.turns_per_dau_per_day = float(per_user["turns"])
        sizes = conn.execute(
            "SELECT (SELECT avg(pg_column_size(t.*)) FROM turns t) AS turn_row, "
            "       (SELECT avg(pg_column_size(h.*)) FROM chat_history h) AS chat_row"
        ).fetchone()
        if sizes and sizes["turn_row"] and sizes["chat_row"]:
            i.bytes_per_turn = float(sizes["turn_row"]) * 1.5 + 2 * float(sizes["chat_row"]) * 1.4
    return i


def build_workbook(i: Inputs, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    inp = wb.active
    inp.title = "Inputs"
    inp.append(["Input", "Value", "Status", "Where it comes from"])
    cell: dict[str, str] = {}
    scalars = [
        "price_in_usd_per_m", "price_out_usd_per_m", "usd_inr", "turns_per_dau_per_day", "peak_hour_share",
        "llm_keys", "llm_requests_per_key_per_day", "trial_tokens_per_user_per_day", "process_turns_per_second",
        "replicas", "db_limit_mb", "db_baseline_mb", "bytes_per_turn", "horizon_months",
    ]  # fmt: skip
    for name in scalars:
        inp.append([name, getattr(i, name), NOTES[name][0], NOTES[name][1]])
        cell[name] = f"Inputs!$B${inp.max_row}"
    inp.append([])
    inp.append(["Per kind of question", "Mix", "calls/turn", "tokens in", "tokens out"])
    inp.cell(row=inp.max_row, column=6, value="status / source")
    first = inp.max_row + 1
    for k in KINDS:
        inp.append(
            [
                k,
                MIX[k],
                i.calls_per_turn[k],
                i.tokens_in[k],
                i.tokens_out[k],
                "calls and tokens in measured; tokens out assumed",
            ]
        )
    last = inp.max_row
    for c in inp[1]:
        c.font = Font(bold=True)
    for c in inp[first - 1]:
        c.font = Font(bold=True)
    amber = PatternFill("solid", fgColor="FFF2CC")
    for row in inp.iter_rows(min_row=2, max_row=last):
        if row[2].value in ("assumed", "verify"):
            row[1].fill = amber
    inp.column_dimensions["A"].width = 32
    inp.column_dimensions["B"].width = 14
    inp.column_dimensions["C"].width = 12
    inp.column_dimensions["D"].width = 90
    inp.column_dimensions["E"].width = 12
    inp.column_dimensions["F"].width = 48

    m = wb.create_sheet("Model")
    m.append(["Cost per turn", "calls", "tokens in", "tokens out", "cost (INR)"])
    for n, k in enumerate(KINDS):
        r = first + n
        m.append([k, f"=Inputs!C{r}", f"=Inputs!D{r}", f"=Inputs!E{r}",
                  f"=(C{m.max_row + 1}*{cell['price_in_usd_per_m']}+D{m.max_row + 1}*{cell['price_out_usd_per_m']})/1000000*{cell['usd_inr']}"])  # fmt: skip
    rows_k = f"{first}:{last}"  # noqa: F841 - documented range
    m.append(["Weighted by the mix", f"=SUMPRODUCT(Inputs!B{first}:B{last},Inputs!C{first}:C{last})",
              f"=SUMPRODUCT(Inputs!B{first}:B{last},Inputs!D{first}:D{last})",
              f"=SUMPRODUCT(Inputs!B{first}:B{last},Inputs!E{first}:E{last})",
              f"=SUMPRODUCT(Inputs!B{first}:B{last},E2:E{1 + len(KINDS)})"])  # fmt: skip
    wrow = m.max_row
    m.append([])
    m.append(["Limit", "DAU at which it is reached", "What lifts it"])
    start = m.max_row + 1
    m.append([
        "Database size lasting the horizon",
        f"=({cell['db_limit_mb']}-{cell['db_baseline_mb']})*1048576/({cell['horizon_months']}*{cell['turns_per_dau_per_day']}*30*{cell['bytes_per_turn']})",
        "A paid Neon plan, or archive old chats",
    ])  # fmt: skip
    m.append([
        "LLM requests per day on the trial keys",
        f"={cell['llm_keys']}*{cell['llm_requests_per_key_per_day']}/({cell['turns_per_dau_per_day']}*B{wrow})",
        "More trial keys, then a paid LLM tier",
    ])  # fmt: skip
    m.append([
        "App CPU (replicas x turns/s, peak hour)",
        f"={cell['process_turns_per_second']}*{cell['replicas']}*3600/{cell['peak_hour_share']}/{cell['turns_per_dau_per_day']}",
        "More replicas (compose --scale app=N)",
    ])  # fmt: skip
    end = m.max_row
    m.append([])
    m.append(["First limit to be hit (DAU)", f"=MIN(B{start}:B{end})"])
    m.append(["Second", f"=SMALL(B{start}:B{end},2)"])
    m.append(["Third", f"=LARGE(B{start}:B{end},1)"])
    m.append([])
    m.append(
        [
            "Trial token cap, turns per user per day",
            f"={cell['trial_tokens_per_user_per_day']}/(C{wrow}+D{wrow})",
        ]
    )
    for r in m.iter_rows(min_row=start, max_row=end, min_col=2, max_col=2):
        r[0].number_format = "#,##0"
    for r in m.iter_rows(min_row=2, max_row=wrow, min_col=5, max_col=5):
        r[0].number_format = "0.00"
    for c in (m[1], m[wrow + 2]):
        for x in c:
            x.font = Font(bold=True)
    m.column_dimensions["A"].width = 44
    m.column_dimensions["B"].width = 30
    m.column_dimensions["C"].width = 42
    for c in "DE":
        m.column_dimensions[c].width = 14
    for row in m.iter_rows():
        for c in row:
            c.alignment = Alignment(wrap_text=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def summary(i: Inputs) -> str:
    lines = ["| Kind | LLM calls | Tokens in | Tokens out | Cost per turn |", "|---|---|---|---|---|"]
    for k in KINDS:
        lines.append(
            f"| {k} | {i.calls_per_turn[k]:.1f} | {i.tokens_in[k]:,.0f} | {i.tokens_out[k]:,.0f} | ₹{cost_per_turn_inr(i, k):.2f} |"
        )
    lines.append(
        f"| **weighted by the mix** | {weighted(i, i.calls_per_turn):.1f} | {weighted(i, i.tokens_in):,.0f} | {weighted(i, i.tokens_out):,.0f} | **₹{sum(MIX[k] * cost_per_turn_inr(i, k) for k in KINDS):.2f}** |"
    )
    lines += ["", "| # | Limit | DAU at which it is reached | What lifts it |", "|---|---|---|---|"]
    for n, r in enumerate(limits(i), 1):
        lines.append(f"| {n} | {r['limit']} | {r['dau']:,.0f} | {r['tier']} |")
    lines += ["", trial_token_cap_note(i)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-db", action="store_true", help="replace the inputs the turns table can measure")
    ap.add_argument("--markdown", action="store_true", help="print the summary tables")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "capacity_model.xlsx")
    args = ap.parse_args(argv)

    import copy

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the rupee sign on a Windows console

    inputs = copy.deepcopy(DEFAULTS)
    if args.from_db:
        from config import DATABASE_URL

        inputs = measure_from_db(inputs, DATABASE_URL)
    build_workbook(inputs, args.out)
    print(f"wrote {args.out}")  # noqa: T201
    if args.markdown or not args.from_db:
        print(summary(inputs))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
