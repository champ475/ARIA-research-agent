"""OPS-02 / TEST-07: the screener CSV has a fixed column order, even when the
rows come back from the evidence cache (JSONB reorders their keys)."""

import csv
import io

from utils.screener_files import screener_csv_bytes


def _header(rows):
    return next(csv.reader(io.StringIO(screener_csv_bytes(rows).decode("utf-8"))))


def test_header_order_does_not_depend_on_key_order():
    live = {"symbol": "TCS", "name": "TCS Ltd", "sector": "Technology", "price": 1.0, "pe": 25.0, "zeta": 1}
    cached = dict(sorted(live.items(), key=lambda kv: (len(kv[0]), kv[0])))  # JSONB's order
    assert _header([live]) == _header([cached]) == ["symbol", "name", "sector", "price", "pe", "zeta"]


def test_keys_missing_from_the_first_row_still_export():
    rows = [{"symbol": "TCS"}, {"symbol": "INFY", "roe": 30.0}]
    assert _header(rows) == ["symbol", "roe"]
    assert screener_csv_bytes([]) == b""
