"""
scripts/pg_tools.py — run pg_dump / pg_restore for the backup scripts (REL-08).

Uses the local PostgreSQL client tools when they're on PATH (a server or the
Docker image with postgresql-client); otherwise runs them from the official
`postgres:17-alpine` image (PG_CLIENT_IMAGE) (client 17 reads and writes dumps of servers
14–17, which covers Neon). Inside a container, "localhost" is the container
itself, so a local URL is rewritten to `host.docker.internal`.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed binaries, argument lists, no shell
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Override with PG_CLIENT_IMAGE (e.g. postgres:16-alpine when that's already pulled
# and the server is ≤ 16); the client must be at least the server's version.
CLIENT_IMAGE = os.environ.get("PG_CLIENT_IMAGE", "postgres:17-alpine")


def _docker() -> str:
    docker = shutil.which("docker")
    if not docker:
        raise SystemExit("Neither pg_dump nor docker is installed — install postgresql-client or Docker.")
    return docker


def container_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        netloc = parts.netloc.replace(host, "host.docker.internal", 1)
        return urlunsplit(parts._replace(netloc=netloc))
    return url


def pg_dump(url: str, out: Path) -> None:
    """Custom-format dump of `url` to `out` (no owners/ACLs, so it restores anywhere)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["--format=custom", "--no-owner", "--no-acl", "--compress=6"]
    local = shutil.which("pg_dump")
    if local:
        subprocess.run([local, *args, f"--file={out}", url], check=True)  # noqa: S603  # nosec B603
        return
    subprocess.run(  # noqa: S603  # nosec B603
        [
            _docker(),
            "run",
            "--rm",
            "-v",
            f"{out.parent.resolve()}:/backup",
            CLIENT_IMAGE,
            "pg_dump",
            *args,
            f"--file=/backup/{out.name}",
            container_url(url),
        ],
        check=True,
    )


def docker(*args: str, capture: bool = False) -> str:
    result = subprocess.run(  # noqa: S603  # nosec B603
        [_docker(), *args], check=True, capture_output=capture, text=True
    )
    return (result.stdout or "").strip() if capture else ""
