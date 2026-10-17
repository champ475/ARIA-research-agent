"""REL-07: independent fetches run side by side under a time budget."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from services import watchlist
from utils.parallel import parallel_map


def test_results_keep_input_order_and_isolate_failures():
    def f(x):
        if x == 2:
            raise ValueError("boom")
        return x * 10

    out = parallel_map(f, [1, 2, 3])
    assert out[0] == (True, 10) and out[2] == (True, 30)
    assert out[1][0] is False and isinstance(out[1][1], ValueError)


def test_empty_input():
    assert parallel_map(lambda x: x, []) == []


def test_runs_concurrently():
    start = time.perf_counter()
    out = parallel_map(lambda _: time.sleep(0.2), list(range(8)), max_workers=8)
    assert all(ok for ok, _ in out)
    assert time.perf_counter() - start < 0.8  # sequential would be 1.6 s


def test_budget_returns_without_waiting_for_the_slow_one():
    release = threading.Event()

    def f(x):
        if x == "slow":
            release.wait(5)
        return x

    start = time.perf_counter()
    out = parallel_map(f, ["fast", "slow"], budget_s=0.3)
    release.set()
    assert time.perf_counter() - start < 1.5
    assert out[0] == (True, "fast")
    assert out[1][0] is False and isinstance(out[1][1], TimeoutError)


def test_logging_context_reaches_workers():
    from utils.logging_setup import turn_id_var

    token = turn_id_var.set("turn-42")
    try:
        assert parallel_map(lambda _: turn_id_var.get(), [1, 2]) == [(True, "turn-42")] * 2
    finally:
        turn_id_var.reset(token)


@pytest.fixture
def slow_quotes():
    """get_info costs 0.1 s, get_ohlcv 0.05 s: a 20-symbol list is 3 s one by one."""
    calls = []

    def info(sym):
        calls.append(sym)
        time.sleep(0.1)
        return {"currentPrice": 100.0, "previousClose": 99.0, "shortName": sym, "fiftyTwoWeekHigh": 120.0}

    def ohlcv(sym, *_):
        time.sleep(0.05)
        import pandas as pd

        return pd.DataFrame()

    watchlist.clear_cache()
    with patch("tools.data.fetcher.get_info", info), patch("tools.data.fetcher.get_ohlcv", ohlcv):
        yield calls
    watchlist.clear_cache()


def test_twenty_symbols_cold_is_much_faster_than_one_by_one(slow_quotes):
    symbols = [f"S{i}" for i in range(20)]
    start = time.perf_counter()
    rows = watchlist.watchlist_rows(symbols)
    elapsed = time.perf_counter() - start
    assert [r[0] for r in rows] == symbols
    assert elapsed < 1.2  # 20 x 0.15 s = 3 s sequentially; 8 workers need about 0.45 s


def test_adding_one_symbol_fetches_only_that_symbol(slow_quotes):
    base = [f"S{i}" for i in range(19)]
    watchlist.watchlist_rows(base)
    slow_quotes.clear()
    start = time.perf_counter()
    rows = watchlist.watchlist_rows([*base, "NEW"])
    assert slow_quotes == ["NEW"]
    assert time.perf_counter() - start < 0.4
    assert rows[-1][0] == "NEW" and rows[-1][2] == "₹100.00"


def test_a_failing_or_slow_symbol_blanks_only_its_own_row(slow_quotes, monkeypatch):
    monkeypatch.setattr(watchlist, "WATCHLIST_BUDGET_SECONDS", 5)

    def flaky(sym):
        if sym == "BAD":
            raise RuntimeError("upstream down")
        return ["ok", sym, "1", "1", "1", "1", "1"]

    with patch.object(watchlist, "_build_row", flaky):
        rows = watchlist.watchlist_rows(["A", "BAD", "B"])
    assert rows[1] == ["BAD", "—", "—", "—", "—", "—", "—"]
    assert rows[0][0] == "ok" and rows[2][0] == "ok"
    with patch.object(watchlist, "_build_row", lambda s: ["ok", s, "", "", "", "", ""]):
        assert watchlist.watchlist_rows(["BAD"])[0][0] == "ok"  # failures are not cached


def test_planning_fetches_the_categories_in_parallel():
    from utils import planning_logic as pl

    def slow(cat, top_n, horizon_years):
        time.sleep(0.2)
        return [{"scheme_name": cat}]

    with (
        patch.object(pl, "get_category_funds", slow),
        patch.object(pl, "format_fund_search_results", lambda funds: str(funds[0]["scheme_name"])),
    ):
        start = time.perf_counter()
        text = pl.fetch_fund_data({"horizon_years": 10})
    assert time.perf_counter() - start < 0.7  # six categories one by one: 1.2 s
    assert text.count("---") >= 6

    def boom(cat, top_n, horizon_years):
        raise RuntimeError("amfi down")

    with patch.object(pl, "get_category_funds", boom):
        assert "Unavailable" in pl.fetch_fund_data({})
