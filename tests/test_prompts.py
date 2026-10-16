"""LLM-07: prompts live in the versioned registry, and turns record their ids."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import prompts

ROOT = Path(__file__).resolve().parents[1]
SCAN = ["agents", "graph", "utils", "tools", "web"]
# Constant names that hold model instructions.
PROMPTY = re.compile(r"(SYSTEM|PROMPT|INSTRUCTION|NOTICE|FIELD_GUIDE)$")


def test_every_registry_file_parses_and_matches_its_name():
    assert prompts.names()
    for name in prompts.names():
        p = prompts.get(name)
        assert p.id.startswith(f"{name}@v") and str(p)


def test_highest_version_wins_and_pinning_works(tmp_path, monkeypatch):
    (tmp_path / "demo.text.v1.md").write_text(
        "---\nname: demo.text\nversion: 1\n---\nold\n", encoding="utf-8"
    )
    (tmp_path / "demo.text.v2.md").write_text(
        "---\nname: demo.text\nversion: 2\n---\nnew\n", encoding="utf-8"
    )
    monkeypatch.setattr(prompts, "_DIR", tmp_path)
    prompts._index.cache_clear()
    prompts.get.cache_clear()
    try:
        assert prompts.get("demo.text") == "new" and prompts.get("demo.text").id == "demo.text@v2"
        assert prompts.get("demo.text", version=1) == "old"
    finally:
        prompts._index.cache_clear()
        prompts.get.cache_clear()


def test_concatenation_keeps_both_ids():
    a, b = prompts.get("reflection.system"), prompts.get("safety.untrusted_notice")
    assert (a + b).id == "reflection.system@v1+safety.untrusted_notice@v1"
    assert ("x" + a).id == a.id and (a + "x").id == a.id
    assert isinstance(
        prompts.get("planning.intake").format(field_guide="g", profile_json="{}", message="m"), str
    )


def _is_text_literal(value: ast.expr) -> bool:
    """A string literal, f-string, or concatenation of them (calls such as
    prompts.get(...) don't count)."""
    if isinstance(value, ast.Constant):
        return isinstance(value.value, str)
    if isinstance(value, ast.JoinedStr):
        return True
    if isinstance(value, ast.BinOp):
        return _is_text_literal(value.left) or _is_text_literal(value.right)
    return False


def _inline_prompt_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and PROMPTY.search(target.id):
                literal = _is_text_literal(node.value)
                if literal:
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} {target.id}")
    return found


def test_no_inline_prompt_constants_outside_the_registry():
    hits = [h for d in SCAN for p in (ROOT / d).rglob("*.py") for h in _inline_prompt_constants(p)]
    assert hits == [], "move these into prompts/:\n" + "\n".join(hits)


def test_turn_records_the_prompt_id(monkeypatch):
    from agents.llm_client import LLMClient
    from utils import tracing

    monkeypatch.setattr("config.ARIA_FAKE_LLM", True)  # no cloud keys needed (CI has none)
    client = LLMClient(prompts.get("planner.system"))
    provider = type("P", (), {"name": "stub", "last_usage": (3, 1)})()
    with tracing.turn("t", 1, None, "q") as rec:
        client._record_llm_call(provider, "prompt", "reply")
    assert rec.prompt_versions == {"planner.system@v1": 1}


def test_docker_image_ships_the_prompt_files():
    """.dockerignore drops *.md; the registry's files must be re-included."""
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "*.md" not in lines or "!prompts/*.md" in lines[lines.index("*.md") :]
