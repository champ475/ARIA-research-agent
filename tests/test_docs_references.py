"""ARCH-06: the docs don't point at files that no longer exist. Every
repo path named in backticks in CLAUDE.md, README.md and docs/*.md must be
real (a renamed module or a moved doc fails here, not in a reader's head)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    *(ROOT / n for n in ("CLAUDE.md", "README.md", "CONTRIBUTING.md", "LEGAL.md")),
    *sorted((ROOT / "docs").glob("*.md")),
]
# Snapshots and records of their time, not maintained docs. PRD_V3.md also
# names files its requirements will create.
HISTORICAL = {"PRD_PRODUCTION_READINESS.md", "PRD_V3.md", "RFC_AGENTIC_RESEARCH_GRAPH.md"}
_TOP = r"(?:agents|graph|tools|utils|web|prompts|scripts|tests|migrations|evals|docs|data|ops|\.github)"
_PATH = re.compile(rf"`({_TOP}/[\w./-]+?|[\w-]+\.(?:py|md|yml|yaml|toml|txt|lock))(?:::[\w.]+|\(\))?`")


def referenced_paths(text: str) -> set[str]:
    out = set()
    for raw in _PATH.findall(text):
        path = raw.rstrip("/.")
        if any(ch in path for ch in "*<{") or path.endswith("/"):
            continue
        out.add(path)
    return out


def _exists(path: str) -> bool:
    if (ROOT / path).exists():
        return True
    # `utils/tracing.turn`: a module path plus an attribute.
    module, _, attr = path.rpartition(".")
    if (
        module
        and attr not in ("py", "md", "yml", "yaml", "toml", "txt", "lock")
        and (ROOT / f"{module}.py").exists()
    ):
        return True
    # A bare file name ("fetcher.py") is fine if it exists somewhere in the repo.
    return "/" not in path and any(p for p in ROOT.rglob(path) if "venv" not in p.parts)


def test_extractor():
    text = "see `utils/db.py::ensure_schema`, `config.py`, `tests/edge_cases/` and `a b` or `x*.py`"
    assert referenced_paths(text) == {"utils/db.py", "config.py", "tests/edge_cases"}


@pytest.mark.parametrize("doc", [d for d in DOCS if d.name not in HISTORICAL], ids=lambda d: d.name)
def test_referenced_paths_exist(doc: Path):
    missing = sorted(p for p in referenced_paths(doc.read_text(encoding="utf-8")) if not _exists(p))
    assert not missing, f"{doc.name} names paths that don't exist: {missing}"


def test_old_prd_is_archived():
    assert not (ROOT / "docs" / "PRD.md").exists()
    assert (ROOT / "docs" / "archive" / "PRD.md").exists()
