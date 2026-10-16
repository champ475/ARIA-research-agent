"""TEST-04: prepare the aria_e2e database — schema at head and a
fundamentals snapshot (from the offline FakeProvider) so screens and peer
comparisons have a universe. Run by tests/e2e/conftest.py with DATABASE_URL
pointing at aria_e2e."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

UNIVERSE = [
    "TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK",
    "ITC", "LT", "MARUTI", "SUNPHARMA", "BHARTIARTL", "ASIANPAINT", "TITAN", "NTPC", "ONGC", "COALINDIA",
]  # fmt: skip


def main() -> int:
    from config import DATABASE_URL
    from scripts.refresh_fundamentals import refresh
    from tools.data.instruments import master
    from tools.data.providers.fake import FakeProvider
    from utils import migrations

    migrations.upgrade(DATABASE_URL)
    migrations.setup_checkpointer_tables(DATABASE_URL)
    m = master()
    items = [(s, m.by_symbol[s].name if s in m.by_symbol else s, "") for s in UNIVERSE]
    refresh(items, workers=1, delay=0, fetch=FakeProvider().quote)
    return 0


if __name__ == "__main__":
    sys.exit(main())
