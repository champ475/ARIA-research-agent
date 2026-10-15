"""
prompts/ — every LLM prompt, versioned, in one place (LLM-07).

Each prompt is a file `prompts/<name>.v<N>.md`:

    ---
    name: query_analyzer.system
    version: 1
    description: what the prompt is for
    ---
    <the prompt text, exactly>

`get(name)` returns the highest version (or `get(name, version=N)` a pinned
one) as a `Prompt` — a `str` that also carries `.id` ("name@vN"). LLMClient
records that id on the turn (OBS-02), so a bad answer leads back to the
prompt version that produced it. Concatenating Prompts keeps both ids
("reflection.system@v1+safety.untrusted_notice@v1").

Changing a prompt: add a new version file (don't edit an old one in place),
run the golden-set eval (`make eval`; re-record fixtures for changed prompts)
— CI's eval job gates every PR, and CODEOWNERS requires review of prompts/.
"""

from __future__ import annotations

import re
import threading
from functools import cache, lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_FILE_RE = re.compile(r"^(?P<name>[a-z0-9_.]+)\.v(?P<version>\d+)\.md$")
_lock = threading.Lock()


class Prompt(str):
    """Prompt text that remembers which registry entry (and version) it is."""

    id: str

    def __new__(cls, text: str, prompt_id: str) -> Prompt:
        obj = super().__new__(cls, text)
        obj.id = prompt_id
        return obj

    def __add__(self, other: str) -> Prompt:
        other_id = getattr(other, "id", None)
        new_id = f"{self.id}+{other_id}" if other_id else self.id
        return Prompt(str.__add__(self, other), new_id)

    def __radd__(self, other: str) -> Prompt:
        other_id = getattr(other, "id", None)
        new_id = f"{other_id}+{self.id}" if other_id else self.id
        return Prompt(other + str(self), new_id)


def _parse(path: Path) -> tuple[dict[str, str], str]:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"{path.name}: missing front matter")
    end = raw.index("\n---\n", 4)
    meta = dict(line.split(":", 1) for line in raw[4:end].splitlines() if ":" in line)
    body = raw[end + len("\n---\n") :]
    if not body.endswith("\n"):
        raise ValueError(f"{path.name}: must end with a newline")
    return {k.strip(): v.strip() for k, v in meta.items()}, body[:-1]


@lru_cache(maxsize=1)
def _index() -> dict[str, dict[int, Path]]:
    found: dict[str, dict[int, Path]] = {}
    for path in _DIR.glob("*.md"):
        m = _FILE_RE.match(path.name)
        if m:
            found.setdefault(m.group("name"), {})[int(m.group("version"))] = path
    return found


@cache
def get(name: str, version: int | None = None) -> Prompt:
    versions = _index().get(name)
    if not versions:
        raise KeyError(f"no prompt named {name!r} in prompts/")
    v = version if version is not None else max(versions)
    meta, text = _parse(versions[v])
    if meta.get("name") != name or int(meta.get("version", -1)) != v:
        raise ValueError(f"{versions[v].name}: front matter doesn't match the file name")
    return Prompt(text, f"{name}@v{v}")


def names() -> list[str]:
    return sorted(_index())
