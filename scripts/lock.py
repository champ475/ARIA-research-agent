"""
scripts/lock.py — regenerate or verify the hashed lock files (SEC-14).

requirements.txt / requirements-dev.txt / requirements-alerts.txt hold the human-edited version ranges;
requirements.lock / requirements-dev.lock are the exact, hashed pins the
Docker image and CI install (`pip install --require-hashes`), resolved for the
production platform (Linux x86_64, Python 3.11) whatever OS runs this.

    python scripts/lock.py                      # add new deps, keep other pins
    python scripts/lock.py --upgrade gradio     # move one package to its newest allowed version
    python scripts/lock.py --upgrade-all        # move everything (then run the full test suite)
    python scripts/lock.py --check              # CI: fail if a lock is stale

Existing pins are kept (each lock is its own constraint file) so a re-run
changes nothing unless an input changed. Needs `uv` on PATH.
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import subprocess  # nosec B404 - runs the fixed `uv` binary with literal arguments
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAIRS = [
    ("requirements.txt", "requirements.lock"),
    ("requirements-dev.txt", "requirements-dev.lock"),
    ("requirements-alerts.txt", "requirements-alerts.lock"),  # REL-05: the alert cron's slim set
]
PLATFORM = ["--python-platform", "x86_64-manylinux_2_28", "--python-version", "3.11"]


def _compile(src: str, out: Path, constraints: list[str], upgrade: list[str], upgrade_all: bool) -> None:
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv is not installed: pip install uv")
    cmd = [uv, "pip", "compile", src, "--generate-hashes", *PLATFORM, "--no-annotate", "-q"]
    cmd += ["--custom-compile-command", "python scripts/lock.py"]
    if upgrade_all:
        cmd.append("--upgrade")
    else:
        for c in constraints:
            if (ROOT / c).exists():
                cmd += ["-c", c]
        for pkg in upgrade:
            cmd += ["--upgrade-package", pkg]
    cmd += ["-o", str(out)]
    subprocess.run(cmd, cwd=ROOT, check=True)  # noqa: S603  # nosec B603


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "==" in line and not line.startswith((" ", "#")):
            name, ver = line.split("==", 1)
            pins[name.lower()] = ver.split()[0]
    return pins


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--upgrade", nargs="*", default=[])
    ap.add_argument("--upgrade-all", action="store_true")
    args = ap.parse_args(argv)

    stale = []
    with tempfile.TemporaryDirectory() as tmp:
        for src, lock in PAIRS:
            out = Path(tmp) / lock if args.check else ROOT / lock
            # The dev lock is also constrained by the runtime lock, so the two
            # never disagree on a shared package.
            constraints = [lock] if lock == "requirements.lock" else ["requirements.lock", lock]
            _compile(src, out, constraints, args.upgrade, args.upgrade_all)
            if args.check:
                old = (ROOT / lock).read_text(encoding="utf-8").splitlines()
                new = out.read_text(encoding="utf-8").splitlines()
                if old != new:
                    stale.append(lock)
                    sys.stdout.writelines(
                        f"{d}\n" for d in difflib.unified_diff(old, new, lock, "fresh", n=0)
                    )

    runtime = _pins(ROOT / "requirements.lock")
    mismatched = sorted(
        p
        for other in ("requirements-dev.lock", "requirements-alerts.lock")
        for p, v in _pins(ROOT / other).items()
        if p in runtime and runtime[p] != v
    )
    if mismatched:
        stale.append(f"version mismatch between locks: {mismatched}")
    if stale:
        sys.stderr.write(f"Stale lock files: {stale}. Run: python scripts/lock.py\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
