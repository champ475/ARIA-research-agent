"""REL-08: backup rotation, Docker URL rewriting and the drill record."""

from __future__ import annotations

from datetime import datetime

from scripts import backup_db, pg_tools, restore_drill
from utils.helpers import IST


def test_rotation_keeps_the_newest(tmp_path):
    for stamp in ("20261010-020000", "20261011-020000", "20261012-020000"):
        (tmp_path / f"aria-{stamp}.dump").write_bytes(b"x")
        (tmp_path / f"aria-{stamp}.dump.sha256").write_text("h")
    removed = backup_db.rotate(tmp_path, keep=2)
    assert [p.name for p in removed] == ["aria-20261010-020000.dump"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "aria-20261011-020000.dump",
        "aria-20261011-020000.dump.sha256",
        "aria-20261012-020000.dump",
        "aria-20261012-020000.dump.sha256",
    ]


def test_backup_writes_a_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(pg_tools, "pg_dump", lambda url, out: out.write_bytes(b"PGDMP fake"))
    out = backup_db.backup(
        "postgresql://u:p@h/db", tmp_path, keep=5, now=IST.localize(datetime(2026, 10, 16, 2))
    )
    assert out.name == "aria-20261016-020000.dump"
    assert (tmp_path / "aria-20261016-020000.dump.sha256").read_text().split()[0] == backup_db.sha256(out)


def test_local_urls_reach_the_host_from_a_container():
    assert (
        pg_tools.container_url("postgresql://a:b@localhost:55432/x")
        == "postgresql://a:b@host.docker.internal:55432/x"
    )
    assert pg_tools.container_url("postgresql://a:b@ep-x.neon.tech/x?sslmode=require").endswith(
        "ep-x.neon.tech/x?sslmode=require"
    )


def test_drill_record(tmp_path, monkeypatch):
    monkeypatch.setattr(restore_drill, "DRILLS", tmp_path)
    result = {
        "counts": {"users": 3},
        "problems": [],
        "schema": "at the latest migration",
        "byo_keys": {"checked": 1, "unreadable": 0},
        "dump": "aria-x.dump",
        "dump_bytes": 1234,
        "restore_seconds": 1.0,
        "total_seconds": 9.0,
        "app_ready": True,
        "source": "seeded test database",
    }
    path = restore_drill.record(result, IST.localize(datetime(2026, 10, 16, 13, 0)))
    text = path.read_text(encoding="utf-8")
    assert (
        path.name == "2026-10-16-restore.md"
        and "**Result: PASSED**" in text
        and "RTO target 4 h: met" in text
    )
    result["problems"] = ["schema: behind"]
    assert "**Result: FAILED**" in restore_drill.record(
        result, IST.localize(datetime(2026, 10, 17))
    ).read_text(encoding="utf-8")
