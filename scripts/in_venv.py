"""
scripts/in_venv.py — run a Python module with the project's own interpreter.

Used by the pre-commit mypy hook: mypy (with the pydantic plugin) must see the
project's installed packages, but a git hook runs with whatever PATH the
committing shell has, and on a machine where the venv isn't activated `mypy`
resolves to a global install with different packages.

    python scripts/in_venv.py mypy            # = <project venv python> -m mypy

Interpreter, in order: ./venv, ./.venv, an activated $VIRTUAL_ENV (unless it
is the one running this script: pre-commit's own hook env sets it), else
`<module>` on PATH (CI, or any shell where the right environment is active).
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - runs a local interpreter with the caller's arguments
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _venv_python() -> Path | None:
    active = os.environ.get("VIRTUAL_ENV", "")
    if active and Path(active).resolve() == Path(sys.prefix).resolve():
        active = ""  # pre-commit's hook env, not the project's
    for base in (str(ROOT / "venv"), str(ROOT / ".venv"), active):
        if not base:
            continue
        for exe in (Path(base) / "Scripts" / "python.exe", Path(base) / "bin" / "python"):
            if exe.is_file():
                return exe
    return None


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: python scripts/in_venv.py <module> [args...]\n")
        return 2
    python = _venv_python()
    if python is not None:
        cmd = [str(python), "-m", *argv]
    elif shutil.which(argv[0]):
        cmd = argv
    else:
        sys.stderr.write(f"{argv[0]}: no project venv (./venv, ./.venv) and not on PATH\n")
        return 1
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode  # noqa: S603  # nosec B603


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
