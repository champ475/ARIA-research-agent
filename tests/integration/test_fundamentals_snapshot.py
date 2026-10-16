"""DATA-05 on a real Postgres: the screener queries a full-universe snapshot
in SQL — fast, matching a manual check, flagging stale data — and the
nightly refresh writes and resumes it."""

from __future__ import annotations

import random
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

SECTORS = ["Technology", "Financial Services", "Healthcare", "Energy", "Industrials", "Consumer Defensive"]


def _seed(db, n: int = 2000) -> list[dict]:
    rng = random.Random(7)
    rows = []
    for i in range(n):
        sector = SECTORS[i % len(SECTORS)]
        lender = sector == "Financial Services" and (i // len(SECTORS)) % 2 == 0

        def maybe(lo, hi, nd=2):
            return None if rng.random() < 0.08 else round(rng.uniform(lo, hi), nd)

        rows.append(
            {
                "symbol": f"S{i:04d}",
                "isin": f"INE{i:06d}01",
                "name": f"Company {i}",
                "sector": sector,
                "industry": "Banks - Regional" if lender else "Other",
                "lender": lender,
                "price": round(rng.uniform(10, 5000), 2),
                "mcap_cr": maybe(50, 500000, 0),
                "pe": maybe(-20, 120),
                "pb": maybe(0.2, 20),
                "roe": maybe(-10, 45),
                "roce": None if lender else maybe(-5, 60),
                "de": None if lender else maybe(0, 4),
                "div_yield": maybe(0, 6),
                "rev_growth": maybe(-30, 60),
                "from_52w_high": maybe(0, 70),
                "from_52w_low": maybe(0, 200),
            }
        )
    assert db.fundamentals_upsert(rows) == n
    return rows


def _manual(rows, pred):
    hits = [r for r in rows if pred(r)]
    hits.sort(key=lambda r: r["mcap_cr"] if r["mcap_cr"] is not None else -1, reverse=True)
    return [r["symbol"] for r in hits]


def test_three_filters_match_a_manual_check_and_run_fast(db):
    rows = _seed(db)

    cases = [
        ([("pe", "<", 20.0, None)], None, lambda r: r["pe"] is not None and r["pe"] < 20),
        (
            [("roe", ">=", 15.0, None), ("de", "<=", 1.0, None)],
            None,
            lambda r: r["roe"] is not None and r["roe"] >= 15 and r["de"] is not None and r["de"] <= 1,
        ),
        (
            [("pb", "between", 1.0, 3.0), ("mcap", ">", 10000.0, None)],
            ["Technology"],
            lambda r: (
                r["sector"] == "Technology"
                and r["pb"] is not None
                and 1 <= r["pb"] <= 3
                and r["mcap_cr"] is not None
                and r["mcap_cr"] > 10000
            ),
        ),
    ]
    for filters, sectors, pred in cases:
        got = [r["symbol"] for r in db.fundamentals_screen(filters, sectors, limit=5000)]
        want = _manual(rows, pred)
        assert len(got) == len(want) and set(got) == set(want), filters
        caps = [r["mcap_cr"] for r in db.fundamentals_screen(filters, sectors, limit=5000) if r["mcap_cr"]]
        assert caps == sorted(caps, reverse=True)

    timings = []
    for _ in range(20):
        t0 = time.perf_counter()
        db.fundamentals_screen([("pe", "<", 25.0, None), ("roe", ">", 12.0, None)], None, limit=50)
        timings.append(time.perf_counter() - t0)
    timings.sort()
    assert timings[int(len(timings) * 0.95) - 1] < 2.0


def test_lenders_have_no_roce_or_de_and_fail_those_filters(db):
    rows = _seed(db, 120)
    lenders = {r["symbol"] for r in rows if r["lender"]}
    got = {r["symbol"] for r in db.fundamentals_screen([("de", "<", 100.0, None)], None, limit=500)}
    assert lenders and not (lenders & got)


def test_unknown_fields_and_operators_are_refused(db):
    with pytest.raises(ValueError):
        db.fundamentals_screen([("pe; DROP TABLE users", "<", 1.0, None)])
    with pytest.raises(ValueError):
        db.fundamentals_screen([("pe", "<>", 1.0, None)])


def test_screener_uses_the_snapshot_and_reports_coverage(db):
    from tools import screener

    _seed(db, 300)
    out = screener.run(screener.parse_query("PE below 15 and ROE above 20"))
    assert out and all(r["source"] == "snapshot" for r in out)
    assert all(r["pe"] <= 15 and r["roe"] >= 20 for r in out)  # shown rounded to 1 dp
    md = screener.to_markdown(out, screener.parse_query("PE below 15 and ROE above 20"))
    assert "Screened 300 NSE stocks" in md and "hours old" not in md

    stale = datetime.now(UTC) + timedelta(hours=30)
    assert "30 hours old" in screener.coverage_note(out, now=stale)


def test_refresh_writes_and_resumes(db):
    import scripts.refresh_fundamentals as rf

    universe = [
        ("TCS", "Tata Consultancy Services Limited", "INE467B01029"),
        ("SBIN", "State Bank of India", "X"),
    ]
    quotes = {
        "TCS": {"currentPrice": 3000.0, "trailingPE": 25.0, "sector": "Technology", "debtToEquity": 9.8},
        "SBIN": {
            "currentPrice": 800.0,
            "trailingPE": 10.0,
            "sector": "Financial Services",
            "industry": "Banks - Regional",
            "debtToEquity": 300.0,
        },
    }
    counts = rf.refresh(universe, workers=2, delay=0, fetch=lambda s: quotes[s])
    assert counts == {"written": 2, "empty": 0, "failed": 0}
    rows = {r["symbol"]: r for r in db.fundamentals_screen([], None, limit=10)}
    assert rows["TCS"]["de"] == 0.1 and rows["SBIN"]["lender"] and rows["SBIN"]["de"] is None
    assert db.fundamentals_status()["rows"] == 2

    with patch.object(rf, "universe", return_value=universe), patch.object(rf, "refresh") as again:
        again.return_value = {"written": 0, "empty": 0, "failed": 0}
        rf.main([])
    assert again.call_args[0][0] == []  # both fresh: nothing left to fetch
    assert db.fundamentals_fresh_symbols(datetime.now(UTC) - timedelta(hours=1)) == {"TCS", "SBIN"}
