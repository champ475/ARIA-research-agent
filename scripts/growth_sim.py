"""
scripts/growth_sim.py — how fast the database grows per active user (DATA-10).

Runs real chat turns (fake LLM + recorded data, so it is offline and free)
against a THROWAWAY database and prints the storage each turn adds, then
projects a week at N daily active users with the retention schedule applied.

    ARIA_NO_DOTENV=1 ARIA_FAKE_LLM=1 ARIA_FAKE_DATA=1 APP_ENV=test \
    DATABASE_URL=postgresql://... SECRET_KEY=... python scripts/growth_sim.py [--dau 100] [--turns 5]

Never point it at a database you care about: it creates a user and chat rows.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QUERIES = [
    "How is TCS doing?",
    "What is the PE of Infosys?",
    "RSI and MACD for HDFC Bank",
    "Compare Reliance and ITC",
    "Latest news on Wipro",
]
TURNS_PER_DAU_PER_DAY = 10
TABLES = [
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "turns",
    "chat_history",
    "chat_sessions",
    "evidence_cache",
    "ohlcv_cache",
    "audit_log",
    "notifications",
]


def sizes(db) -> dict[str, int]:
    rows = db._query(
        "SELECT relname, pg_total_relation_size(c.oid) AS bytes FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' "
        "AND relname = ANY(%s)",
        (TABLES,),
    )
    return {r["relname"]: int(r["bytes"]) for r in rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dau", type=int, default=100)
    ap.add_argument("--turns", type=int, default=len(QUERIES))
    args = ap.parse_args(argv)

    from agents.graph_orchestrator import GraphOrchestrator
    from config import APP_ENV, check_boot
    from utils import db, migrations
    from utils.logging_setup import configure_logging

    if APP_ENV not in ("test", "dev"):
        raise SystemExit("refusing to run outside APP_ENV=test|dev")
    configure_logging()
    logging.getLogger().setLevel(logging.WARNING)
    check_boot(serving=False)
    migrations.upgrade(db.DATABASE_URL)
    migrations.setup_checkpointer_tables(db.DATABASE_URL)

    user = db._query(
        "INSERT INTO users (email, password_hash) VALUES (%s,'x') ON CONFLICT (email) DO UPDATE SET email=EXCLUDED.email "
        "RETURNING id",
        ("growth-sim@example.com",),
        fetch="one",
        commit=True,
    )["id"]
    session = db._query(
        "INSERT INTO chat_sessions (user_id, tab) VALUES (%s,'chat') RETURNING id",
        (user,),
        fetch="one",
        commit=True,
    )["id"]
    orch = GraphOrchestrator(user, session)

    before = sizes(db)
    n = 0
    for i in range(args.turns):
        for _ in orch.process_stream(QUERIES[i % len(QUERIES)]):
            pass
        n += 1
    after = sizes(db)
    delta = {t: after.get(t, 0) - before.get(t, 0) for t in TABLES}
    total = sum(delta.values())
    per_turn = total / max(n, 1)
    print(f"{n} turns, {total / 1024:.0f} KiB added -> {per_turn / 1024:.1f} KiB per turn")
    for t, b in sorted(delta.items(), key=lambda kv: -kv[1]):
        if b:
            print(f"  {t:<18} {b / 1024:8.1f} KiB  ({b / max(n, 1) / 1024:.1f} KiB/turn)")

    # Steady state with retention: checkpoints/evidence live 7 days; chat rows and turns
    # grow until their (much longer) limits, so the *weekly growth* is the whole per-turn cost.
    week = args.dau * TURNS_PER_DAU_PER_DAY * 7 * per_turn
    print(
        f"\n{args.dau} DAU x {TURNS_PER_DAU_PER_DAY} turns/day x 7 days = "
        f"{week / 1024 / 1024:.1f} MiB/week before retention"
    )
    kept = sum(delta[t] for t in ("chat_history", "chat_sessions", "turns", "audit_log", "notifications"))
    print(
        f"retained growth (checkpoints and caches are purged after 7 days): "
        f"{args.dau * TURNS_PER_DAU_PER_DAY * 7 * kept / max(n, 1) / 1024 / 1024:.1f} MiB/week"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
