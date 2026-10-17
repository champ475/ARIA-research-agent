"""COMP-05: every outside data source and fetcher is in docs/DATA_SOURCES.md,
so a new scrape can't ship without its terms and attribution written down."""

from __future__ import annotations

import re
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parents[1]
REGISTER = (ROOT / "docs" / "DATA_SOURCES.md").read_text(encoding="utf-8")
CODE_DIRS = ["tools", "utils", "graph", "agents", "web", "scripts"]
CODE_FILES = ["app.py", "server.py", "worker.py"]
# `source=` inside an http.get/post call (arguments may nest parentheses two
# deep, e.g. URL.format(d=d.strftime(...))), or the first argument of a
# session / guarded call / feed fetch.
_NESTED = r"(?:[^()]|\((?:[^()]|\([^()]*\))*\))*?"
_BREAKER = re.compile(
    rf"""(?:http\.(?:get|post)\({_NESTED}source=|http\.session\(|guarded_call\(|_fetch_feed\([^,]+,\s*)"""
    r"""["']([a-z_0-9:]+)["']"""
)
# tools/data modules that never leave the process (cache, schemas).
INTERNAL = {"__init__.py", "cache.py", "contracts.py", "evidence_cache.py"}


def _code() -> list[Path]:
    files = [p for d in CODE_DIRS for p in (ROOT / d).rglob("*.py")]
    return files + [ROOT / f for f in CODE_FILES if (ROOT / f).exists()]


def breaker_sources() -> set[str]:
    found: set[str] = set()
    for path in _code():
        found |= set(_BREAKER.findall(path.read_text(encoding="utf-8")))
    found |= {f"rss:{name}" for name in config.NEWS_FEEDS}
    return found


def test_scan_sees_the_known_sources():
    assert {"yfinance", "nse", "bse", "amfi", "mfapi", "google_news", "telegram"} <= breaker_sources()


def test_every_breaker_source_is_registered():
    missing = sorted(s for s in breaker_sources() if f"`{s}`" not in REGISTER)
    assert not missing, f"add these to docs/DATA_SOURCES.md: {missing}"


def test_every_fetcher_module_is_registered():
    modules = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "tools" / "data").rglob("*.py")
        if p.name not in INTERNAL and "__pycache__" not in p.parts
    ]
    missing = sorted(m for m in modules if f"`{m}`" not in REGISTER)
    assert not missing, f"add these to docs/DATA_SOURCES.md: {missing}"


def test_register_has_status_and_attribution_for_each_source():
    for heading in ("Market and fund data", "News"):
        section = REGISTER.split(f"## {heading}")[1].split("\n## ")[0]
        rows = [r for r in section.splitlines() if r.startswith("| `")]
        assert rows
        for row in rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            assert len(cells) == 7 and all(cells), row


ATTRIBUTION = "Data: Yahoo Finance (via yfinance), NSE, BSE, AMFI, mfapi.in, Google News and publisher RSS"


def test_attribution_line_is_the_same_in_app_readme_and_register():
    from tests.conftest import import_app_without_side_effects

    app = import_app_without_side_effects()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert ATTRIBUTION in app.FOOTER_HTML and ATTRIBUTION in readme and ATTRIBUTION in REGISTER
