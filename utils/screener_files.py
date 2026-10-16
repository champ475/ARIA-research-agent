"""
utils/screener_files.py — screener results as CSV bytes (OPS-02: stored in
the `downloads` table and served by /download/file/<token>, never written
to a replica's temp dir).
"""

from __future__ import annotations

import csv
import io

# Column order of the export. Rows can't be trusted to carry it: a repeat
# query is served from the evidence cache, whose JSONB storage reorders keys.
COLUMNS = [
    "symbol",
    "name",
    "sector",
    "price",
    "mktcap_cr",
    "pe",
    "pb",
    "roe",
    "roce",
    "de",
    "div_yield",
    "rev_growth",
    "as_of",
    "source",
]


def screener_csv_bytes(results: list[dict]) -> bytes:
    if not results:
        return b""
    keys = {k for row in results for k in row}
    fields = [c for c in COLUMNS if c in keys] + sorted(keys - set(COLUMNS))
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(results)
    return out.getvalue().encode("utf-8")
