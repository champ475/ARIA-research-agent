"""
scripts/backup_db.py — nightly logical backup of the ARIA database (REL-08).

    python scripts/backup_db.py                      # DATABASE_URL -> BACKUP_DIR, keep 14
    python scripts/backup_db.py --keep 30 --out /mnt/backups

Writes `aria-<YYYYMMDD-HHMMSS>.dump` (pg_dump custom format, no owners/ACLs)
plus a `.sha256` next to it, then deletes all but the newest `--keep`
dumps. Copy BACKUP_DIR off the machine (object storage, another disk) —
a backup on the same host as the database is not a backup.

On Neon, point-in-time restore (7 days on the free tier) is the first line
of recovery; these dumps are the independent copy (RPO ≤ 24 h). Restore
steps: docs/RUNBOOK.md#backups; prove they work: scripts/restore_drill.py.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("backup_db")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rotate(out_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` dumps (and their checksums)."""
    dumps = sorted(out_dir.glob("aria-*.dump"))
    removed = dumps[:-keep] if keep > 0 else []
    for d in removed:
        d.unlink(missing_ok=True)
        d.with_suffix(".dump.sha256").unlink(missing_ok=True)
    return removed


def backup(url: str, out_dir: Path, keep: int, now: datetime | None = None) -> Path:
    from scripts.pg_tools import pg_dump
    from utils.helpers import IST

    stamp = (now or datetime.now(IST)).strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"aria-{stamp}.dump"
    pg_dump(url, out)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("pg_dump produced no output")
    out.with_suffix(".dump.sha256").write_text(f"{sha256(out)}  {out.name}\n", encoding="utf-8")
    removed = rotate(out_dir, keep)
    log.info("backup %s (%d bytes); rotated out %d old dump(s)", out.name, out.stat().st_size, len(removed))
    return out


def main(argv: list[str] | None = None) -> int:
    from config import BACKUP_DIR, DATABASE_URL
    from utils.logging_setup import configure_logging

    configure_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DATABASE_URL, help="database to back up (default DATABASE_URL)")
    ap.add_argument("--out", default=BACKUP_DIR)
    ap.add_argument("--keep", type=int, default=14)
    args = ap.parse_args(argv)
    if not args.url:
        raise SystemExit("No database URL: set DATABASE_URL or pass --url.")
    backup(args.url, Path(args.out), args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
