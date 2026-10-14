"""
scripts/cleanup_garbage_alerts.py — one-time audit for alert rows created
before the entity-validation fix (utils/helpers.py::filter_valid_entities,
graph/nodes/query_analyzer.py). Before that fix, any capitalized English
word in a message could be written to the `alerts` table as a symbol —
confirmed in production: a single multi-intent chat message inserted real
alert rows for "AN", "SET", and "ALSO" alongside one legitimate "INFY" row.

This script only LISTS suspect rows for manual review. It never deletes
anything unless the operator explicitly passes the exact row IDs to delete
after reviewing the printed list — a blanket "delete anything not in the
curated universe" is NOT safe, since a real user could have a legitimate
alert on a ticker not yet in STOCK_UNIVERSE (see filter_valid_entities'
bounded yfinance-fallback path for why that case is intentionally allowed
to exist).

Usage:
    python scripts/cleanup_garbage_alerts.py                      # list only
    python scripts/cleanup_garbage_alerts.py --delete-confirmed 75,76,77
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL, STOCK_UNIVERSE
from utils.helpers import TICKER_ALIASES

_KNOWN_SYMBOLS = set(STOCK_UNIVERSE) | set(TICKER_ALIASES.values())


def _fetch_all_alerts(conn) -> list[dict]:
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id, user_id, symbol, alert_type, condition, threshold, active, created_at "
        "FROM alerts ORDER BY created_at"
    )
    return cur.fetchall()


def _delete_by_ids(conn, ids: list[int]) -> int:
    cur = conn.cursor()
    cur.execute("DELETE FROM alerts WHERE id = ANY(%s)", (ids,))
    deleted = cur.rowcount
    conn.commit()
    return deleted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delete-confirmed", type=str, default=None,
        help="Comma-separated alert IDs to delete, e.g. 75,76,77. "
             "Only IDs you explicitly list here are ever deleted.",
    )
    args = parser.parse_args()

    conn = psycopg2.connect(DATABASE_URL)
    try:
        rows = _fetch_all_alerts(conn)
        suspect = [r for r in rows if r["symbol"] not in _KNOWN_SYMBOLS]

        if not suspect:
            print(f"Checked {len(rows)} alert rows — none look suspect.")
            return

        print(f"Checked {len(rows)} alert rows — {len(suspect)} not in the curated "
              f"universe/alias map (review before deleting; a legitimate ticker not "
              f"yet curated can look suspect too):\n")
        print(f"{'id':>5}  {'user_id':>7}  {'symbol':<12} {'alert_type':<18} "
              f"{'threshold':>10}  created_at")
        for r in suspect:
            print(f"{r['id']:>5}  {r['user_id']:>7}  {r['symbol']:<12} "
                  f"{r['alert_type']:<18} {str(r['threshold']):>10}  {r['created_at']}")

        if args.delete_confirmed:
            ids = [int(x) for x in args.delete_confirmed.split(",") if x.strip()]
            unlisted = set(ids) - {r["id"] for r in suspect}
            if unlisted:
                print(f"\nRefusing to delete IDs not in the printed suspect list: {sorted(unlisted)}")
                return
            deleted = _delete_by_ids(conn, ids)
            print(f"\nDeleted {deleted} row(s): {ids}")
        else:
            print("\nNo deletion performed. Re-run with "
                  "--delete-confirmed <id1,id2,...> to remove specific rows above.")
    finally:
        conn.close()


if __name__ == "__main__":
    from utils.logging_setup import configure_logging

    configure_logging()
    main()
