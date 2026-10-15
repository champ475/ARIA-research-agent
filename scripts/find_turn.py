"""
scripts/find_turn.py — look up a user-reported Error ID (OBS-02).

Every chat turn writes one `turns` row; the error ID shown to users is the
first 8 hex characters of its turn id. This prints the matching turn(s) and,
when LangSmith tracing was on, the link to the full trace.

Usage: python scripts/find_turn.py 3f9a1c2b
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    from utils.db import turns_by_error_id

    rows = turns_by_error_id(argv[0])
    if not rows:
        print(f"No turn with error ID {argv[0]!r}.")
        return 1
    for row in rows:
        trace = row.pop("trace_url", None)
        print(json.dumps(row, indent=2, default=str))
        print(f"Trace: {trace or '(tracing was off for this turn — see the JSON logs for its turn_id)'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
