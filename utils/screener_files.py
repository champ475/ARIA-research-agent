"""
utils/screener_files.py — screener results as CSV bytes (OPS-02: stored in
the `downloads` table and served by /download/file/<token>, never written
to a replica's temp dir).
"""

from __future__ import annotations

import csv
import io


def screener_csv_bytes(results: list[dict]) -> bytes:
    if not results:
        return b""
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=list(results[0].keys()))
    writer.writeheader()
    writer.writerows(results)
    return out.getvalue().encode("utf-8")
