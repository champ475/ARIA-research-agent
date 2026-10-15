"""
utils/terms.py — which Terms of Use version is current (COMP-02).

LEGAL.md carries a `**Version:** <id>` line; `terms_versions` (migration
0010 and later) records each published version and whether it is material.
A user must have accepted the latest *material* version to use the app; the
session lookup computes that (utils/db.py::auth_session_lookup).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

LEGAL_PATH = Path(__file__).resolve().parent.parent / "LEGAL.md"
_VERSION_RE = re.compile(r"\*\*Version:\*\*\s*([0-9A-Za-z.\-]+)")


@lru_cache(maxsize=1)
def current_version() -> str:
    """The version of the LEGAL.md this code ships with — what a user
    accepting the terms today is accepting."""
    try:
        m = _VERSION_RE.search(LEGAL_PATH.read_text(encoding="utf-8"))
    except OSError:
        m = None
    if not m:
        raise RuntimeError("LEGAL.md has no '**Version:** <id>' line")
    return m.group(1)
