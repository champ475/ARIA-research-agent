"""
scripts/seed_staging.py — synthetic, PII-free data for dev and staging (OPS-01).

    python scripts/seed_staging.py                 # 5 users
    python scripts/seed_staging.py --users 20

Creates verified users `demo1@staging.example.test` ... (password from
--password, default "staging-demo-pass"), each with a watchlist, a few
alerts, portfolio lots, a saved financial profile and a short chat. Every
value is made up: no name, email, phone or real holding of a real person.
Re-running skips users that already exist.

Refuses to run when APP_ENV=prod or the database host is listed in
PROD_DATABASE_HOSTS — seeding must never touch real users' data.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("seed_staging")

SYMBOLS = ["TCS", "INFY", "RELIANCE", "HDFCBANK", "ICICIBANK", "ITC", "LT", "SBIN", "MARUTI", "SUNPHARMA"]
QUESTIONS = [
    ("How is TCS doing?", "TCS trades at a P/E of 25 on the figures on record."),
    ("Compare TCS with peers", "TCS P/E 25.0 vs peer median 22.0 (4 peers, industry: IT Services)."),
    ("Top gainers today", "Here are today's top gainers on NSE."),
]


class Refused(SystemExit):
    pass


def guard() -> None:
    from config import get_settings, prod_db_problem

    s = get_settings()
    if s.APP_ENV == "prod":
        raise Refused("seed_staging refuses to run with APP_ENV=prod.")
    problem = prod_db_problem(s)
    if problem:
        raise Refused(problem)


def seed_user(n: int, password: str, rng: random.Random) -> int | None:
    from utils import auth, db

    email = f"demo{n}@staging.example.test"
    if db.user_by_email(email):
        return None
    user_id = auth.signup(email, password)
    if user_id is None:
        return None
    db.user_mark_verified(user_id)
    db.accept_terms(user_id)
    picks = rng.sample(SYMBOLS, 4)
    for sym in picks:
        db.watchlist_add(user_id, sym)
    db.alert_add(user_id, picks[0], "price_above", f"{picks[0]} above 5000", 5000.0)
    db.alert_add(user_id, picks[1], "pct_change", f"{picks[1]} moves 5%", 5.0, direction="down")
    for sym in picks[:3]:
        db.portfolio_add(
            user_id,
            sym,
            Decimal(rng.randint(1, 50)),
            Decimal(rng.randint(500, 4000)) + Decimal("0.25"),
            date(2025, 1, 1) + timedelta(days=rng.randint(0, 400)),
        )
    db.profile_upsert(
        user_id,
        {
            "age": rng.randint(24, 60),
            "risk_tolerance": rng.choice(["conservative", "moderate", "aggressive"]),
        },
    )
    sid = db.session_create(user_id, "chat", title="Demo chat")
    for q, a in QUESTIONS[: rng.randint(1, 3)]:
        db.chat_save(user_id, sid, "chat", "user", q)
        db.chat_save(user_id, sid, "chat", "assistant", a)
    return user_id


def main(argv: list[str] | None = None) -> int:
    from utils.logging_setup import configure_logging

    configure_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=5)
    ap.add_argument("--password", default="staging-demo-pass")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    guard()
    rng = random.Random(args.seed)  # noqa: S311  # nosec B311 - synthetic demo data, not security
    created = [uid for n in range(1, args.users + 1) if (uid := seed_user(n, args.password, rng))]
    log.info("seeded %d new demo user(s); %d already existed", len(created), args.users - len(created))
    return 0


if __name__ == "__main__":
    sys.exit(main())
