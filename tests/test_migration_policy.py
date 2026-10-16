"""TEST-07: migrations are expand/contract, so a rollback to the previous
image works on the newer schema. An upgrade that removes or rewrites
something (drop, rename, type change) is a *contract* step: it must say so
in its docstring ("CONTRACT:") and ship a release after the code stopped
using it (docs/RELEASING.md). Revisions up to 0021 predate the rule."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
GRANDFATHERED = 21
_DESTRUCTIVE = re.compile(
    r"\bDROP\s+(TABLE|COLUMN)\b|\bRENAME\b|\bALTER\s+COLUMN\s+\w+\s+(SET\s+DATA\s+)?TYPE\b"
    r"|op\.(drop_table|drop_column|alter_column|rename_table)\(",
    flags=re.I,
)


def destructive_upgrade(source: str) -> bool:
    tree = ast.parse(source)
    upgrade = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None)
    return bool(upgrade and _DESTRUCTIVE.search(ast.get_source_segment(source, upgrade) or ""))


def test_detector():
    assert destructive_upgrade('def upgrade():\n    op.execute("ALTER TABLE t DROP COLUMN c")\n')
    assert destructive_upgrade('def upgrade():\n    op.alter_column("t", "c", type_=X)\n')
    assert not destructive_upgrade(
        'def upgrade():\n    op.execute("ALTER TABLE t ADD COLUMN c int")\n'
        'def downgrade():\n    op.execute("ALTER TABLE t DROP COLUMN c")\n'
    )


@pytest.mark.parametrize("path", sorted(VERSIONS.glob("[0-9][0-9][0-9][0-9]_*.py")), ids=lambda p: p.stem)
def test_destructive_upgrades_are_declared_contract_steps(path: Path):
    if int(path.name[:4]) <= GRANDFATHERED:
        return
    source = path.read_text(encoding="utf-8")
    if destructive_upgrade(source):
        doc = ast.get_docstring(ast.parse(source)) or ""
        assert "CONTRACT:" in doc, f"{path.name}: destructive upgrade without a CONTRACT: note"
