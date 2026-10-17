"""
scripts/restore_drill.py — prove a backup restores into a working app (REL-08).

    python scripts/restore_drill.py                         # newest dump in BACKUP_DIR
    python scripts/restore_drill.py --dump backups/aria-20261016-0200.dump
    python scripts/restore_drill.py --from-url $STAGING_DATABASE_URL   # dump first, then drill

1. Starts a throwaway Postgres (Docker, random port) — never the source DB.
2. pg_restore's the dump into it and times everything (RTO).
3. Checks the restored database: schema at the latest migration, row counts
   of the main tables, the app's readiness check passing against it, and —
   with the same SECRET_KEY — stored BYO keys still decrypting (secret escrow).
4. Writes docs/drills/<date>-restore.md and removes the container.

Run quarterly, and after any change to the schema or the backup job. Point
--from-url at staging or a seeded database, not production, unless that is
the drill you mean to run.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("restore_drill")

ROOT = Path(__file__).resolve().parents[1]
DRILLS = ROOT / "docs" / "drills"
TABLES = [
    "users",
    "sessions",
    "chat_sessions",
    "chat_history",
    "watchlist",
    "alerts",
    "portfolio",
    "reports",
    "turns",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(name: str, timeout: float = 60.0) -> None:
    from scripts.pg_tools import docker

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            docker("exec", name, "pg_isready", "-U", "drill", "-d", "restored", capture=True)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("throwaway Postgres did not become ready")


def checks(url: str) -> dict[str, Any]:
    """What a working app needs from the restored database."""
    import psycopg
    from psycopg.rows import dict_row

    from utils import migrations

    out: dict[str, Any] = {"counts": {}, "problems": []}
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        try:
            migrations.check_schema(conn)
            out["schema"] = "at the latest migration"
        except migrations.SchemaOutOfDate as e:
            out["schema"] = "behind"
            out["problems"].append(f"schema: {e}")
        for table in TABLES:
            row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()  # noqa: S608 (fixed names)  # nosec B608
            out["counts"][table] = int(row["n"]) if row else 0
        keys = conn.execute("SELECT api_key_encrypted FROM user_llm_config LIMIT 20").fetchall()
    from utils.crypto import decrypt

    unreadable = 0
    for k in keys:
        try:
            decrypt(k["api_key_encrypted"])
        except Exception:
            unreadable += 1
    out["byo_keys"] = {"checked": len(keys), "unreadable": unreadable}
    if unreadable:
        out["problems"].append(f"{unreadable} stored BYO key(s) don't decrypt with this SECRET_KEY")
    return out


def app_ready(url: str) -> str | None:
    """The app's own readiness check (OPS-04) against the restored database."""
    import config
    from utils import db
    from web import ops_routes

    saved = (config.DATABASE_URL, db.DATABASE_URL, db._pool)
    config.DATABASE_URL = db.DATABASE_URL = url
    db._pool = None
    try:
        return ops_routes._check_database()
    finally:
        db.close_pool()
        config.DATABASE_URL, db.DATABASE_URL, db._pool = saved


def drill(dump: Path, keep_container: bool = False) -> dict[str, Any]:
    from scripts.pg_tools import CLIENT_IMAGE, docker

    started = time.monotonic()
    name = f"aria-restore-drill-{int(time.time())}"
    port = _free_port()
    docker(
        "run", "-d", "--name", name, "-e", "POSTGRES_USER=drill", "-e", "POSTGRES_PASSWORD=drill",
        "-e", "POSTGRES_DB=restored", "-p", f"127.0.0.1:{port}:5432", CLIENT_IMAGE, capture=True,
    )  # fmt: skip
    try:
        _wait_ready(name)
        docker("cp", str(dump), f"{name}:/var/lib/postgresql/restore.dump")
        t0 = time.monotonic()
        docker(
            "exec",
            name,
            "pg_restore",
            "-U",
            "drill",
            "-d",
            "restored",
            "--no-owner",
            "--no-acl",
            "/var/lib/postgresql/restore.dump",
        )
        restore_s = time.monotonic() - t0
        url = f"postgresql://drill:drill@127.0.0.1:{port}/restored"
        result = checks(url)
        ready = app_ready(url)
        if ready:
            result["problems"].append(f"readiness: {ready}")
        result.update(
            dump=dump.name,
            dump_bytes=dump.stat().st_size,
            restore_seconds=round(restore_s, 1),
            total_seconds=round(time.monotonic() - started, 1),
            app_ready=ready is None,
        )
        return result
    finally:
        if not keep_container:
            docker("rm", "-f", name, capture=True)


def record(result: dict[str, Any], when: datetime) -> Path:
    DRILLS.mkdir(parents=True, exist_ok=True)
    path = DRILLS / f"{when:%Y-%m-%d}-restore.md"
    ok = not result["problems"] and result["app_ready"]
    counts = "\n".join(f"| {t} | {n} |" for t, n in result["counts"].items())
    rto_ok = result["total_seconds"] < 4 * 3600
    path.write_text(
        f"""# Restore drill — {when:%d %b %Y, %H:%M} IST

**Result: {"PASSED" if ok else "FAILED"}** — {"restored data serves a working app" if ok else "see problems below"}.

| | |
|---|---|
| Source | {result.get("source") or "—"} |
| Dump | `{result["dump"]}` ({result["dump_bytes"]:,} bytes) |
| Restore time | {result["restore_seconds"]} s |
| Total drill time (start → checks passed) | {result["total_seconds"]} s — RTO target 4 h: {"met" if rto_ok else "MISSED"} |
| Schema | {result.get("schema")} |
| App readiness (`/readyz` database check) | {"pass" if result["app_ready"] else "fail"} |
| Stored BYO keys decrypt with the escrowed SECRET_KEY | {result["byo_keys"]["checked"] - result["byo_keys"]["unreadable"]} of {result["byo_keys"]["checked"]} |

## Rows restored

| Table | Rows |
|---|---|
{counts}

## Problems

{chr(10).join(f"- {p}" for p in result["problems"]) or "None."}

*Written by `scripts/restore_drill.py` (REL-08). Restore steps: docs/RUNBOOK.md#backups.*
""",
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    from config import BACKUP_DIR
    from utils.helpers import IST
    from utils.logging_setup import configure_logging

    configure_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", help="dump file (default: newest in BACKUP_DIR)")
    ap.add_argument("--from-url", help="take a fresh dump of this database first")
    ap.add_argument("--keep-container", action="store_true")
    ap.add_argument(
        "--source", default="", help="what the dump came from, for the record (e.g. 'Neon prod PITR branch')"
    )
    args = ap.parse_args(argv)
    when = datetime.now(IST)
    if args.from_url:
        from scripts.backup_db import backup

        dump = backup(args.from_url, Path(BACKUP_DIR), keep=14, now=when)
    elif args.dump:
        dump = Path(args.dump)
    else:
        dumps = sorted(Path(BACKUP_DIR).glob("aria-*.dump"))
        if not dumps:
            raise SystemExit(f"No dumps in {BACKUP_DIR}; run scripts/backup_db.py or pass --dump/--from-url.")
        dump = dumps[-1]
    result = drill(dump, args.keep_container)
    result["source"] = args.source or (
        f"fresh dump of {args.from_url.split('@')[-1]}" if args.from_url else dump.name
    )
    path = record(result, when)
    log.info("restore drill %s -> %s", "passed" if not result["problems"] else "FAILED", path)
    return 0 if not result["problems"] and result["app_ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
