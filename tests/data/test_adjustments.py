"""DATA-07: split / bonus adjustment of the stored (unadjusted) bhavcopy history."""

from __future__ import annotations

import pandas as pd
import pytest

from tools.data import adjustments as adj
from tools.data import bhavcopy_history as bh

SPLIT = "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"


@pytest.mark.parametrize(
    "subject,factor",
    [
        (SPLIT, 5.0),
        ("Face Value Split (Sub-Division) - From Rs 2/- Per Share To Rs 1/- Per Share", 2.0),
        ("Face Value Split (Sub-Division) - From Rs. 5/- Per Share To Re. 1/- Per Share", 5.0),
        ("Bonus 1:1", 2.0),
        ("Bonus 4:1", 5.0),
        ("Bonus 3:2", 2.5),
        ("Dividend - Rs 12 Per Share", None),
        ("Rights 1:5 @ Premium Rs 100", None),
        ("", None),
    ],
)
def test_parse_factor(subject, factor):
    assert adj.parse_factor(subject) == factor


def _series(prices: list[float], start="2025-06-02") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(prices))
    return pd.DataFrame(
        {"Open": prices, "High": [p * 1.01 for p in prices], "Low": [p * 0.99 for p in prices],
         "Close": prices, "Volume": [1000] * len(prices)},
        index=idx,
    )  # fmt: skip


def _actions(*rows):
    return pd.DataFrame([{"subject": s, "exDate": d} for s, d in rows])


# three stocks: a split, a bonus, and a split followed by a bonus
CASES = [
    ("SPLIT5", [1000, 1010, 990, 1005, 200, 202, 198, 201], [(SPLIT, "06-Jun-2025")]),
    ("BONUS11", [500, 505, 495, 502, 251, 250, 252, 249], [("Bonus 1:1", "06-Jun-2025")]),
    (
        "BOTH",
        [8000, 8100, 7900, 4025, 4000, 802, 800, 802],
        [("Face Value Split (Sub-Division) - From Rs 2/- Per Share To Rs 1/- Per Share", "05-Jun-2025"),
         ("Bonus 4:1", "09-Jun-2025")],
    ),
]  # fmt: skip


@pytest.mark.parametrize("name,prices,rows", CASES)
def test_no_fake_crash_after_adjustment(name, prices, rows):
    raw = _series(prices)
    assert adj.unadjusted_jumps(raw), "the raw series should show the fake crash"
    fixed = adj.adjust_ohlcv(raw, adj.factors_from_actions(_actions(*rows)))
    assert adj.unadjusted_jumps(fixed) == []
    assert fixed["Close"].pct_change().abs().max() < 0.05


def test_52_week_range_uses_adjusted_prices():
    name, prices, rows = CASES[0]
    fixed = adj.adjust_ohlcv(_series(prices), adj.factors_from_actions(_actions(*rows)))
    assert fixed["High"].max() < 250  # not the pre-split 1010 * 1.01
    assert fixed["Close"].iloc[-1] == 201  # latest bar untouched


def test_volume_scales_up_and_original_is_untouched():
    raw = _series(CASES[0][1])
    fixed = adj.adjust_ohlcv(raw, [(pd.Timestamp("2025-06-06"), 5.0)])
    assert fixed["Volume"].iloc[0] == 5000 and fixed["Volume"].iloc[-1] == 1000
    assert raw["Close"].iloc[0] == 1000


def test_no_factors_is_identity():
    raw = _series([100, 101, 102])
    assert adj.adjust_ohlcv(raw, []) is raw


def test_bars_on_or_after_ex_date_are_not_adjusted():
    raw = _series([100, 100, 100, 100])
    out = adj.adjust_ohlcv(raw, [(raw.index[2], 2.0)])
    assert list(out["Close"]) == [50, 50, 100, 100]


def test_real_move_is_not_hidden():
    raw = _series([100, 101, 60, 61])  # -40% with no corporate action: a genuine crash
    assert adj.adjust_ohlcv(raw, []).equals(raw)
    assert adj.unadjusted_jumps(raw)


def test_read_history_adjusts_on_read_and_stores_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(bh, "_HISTORY_DIR", str(tmp_path))
    raw = _series(CASES[0][1])
    raw.to_parquet(tmp_path / "SPLITCO.parquet")
    monkeypatch.setattr(adj, "corporate_factors", lambda s: [(pd.Timestamp("2025-06-06"), 5.0)])
    assert bh.read_history("SPLITCO")["Close"].iloc[0] == 200
    assert bh.read_history("SPLITCO", adjust=False)["Close"].iloc[0] == 1000
    assert pd.read_parquet(tmp_path / "SPLITCO.parquet")["Close"].iloc[0] == 1000  # still raw on disk


def test_corporate_factors_caches_and_survives_failure(monkeypatch):
    import tools.data.nse_scraper as nse

    adj.clear_cache()
    calls = []

    def fake(symbol):
        calls.append(symbol)
        return _actions((SPLIT, "06-Jun-2025"))

    monkeypatch.setattr(nse, "get_corporate_actions", fake)
    assert adj.corporate_factors("abc") == adj.corporate_factors("ABC") == [(pd.Timestamp("2025-06-06"), 5.0)]
    assert calls == ["ABC"]

    adj.clear_cache()
    monkeypatch.setattr(nse, "get_corporate_actions", lambda s: (_ for _ in ()).throw(RuntimeError("nse down")))
    assert adj.corporate_factors("XYZ") == []
    adj.clear_cache()
