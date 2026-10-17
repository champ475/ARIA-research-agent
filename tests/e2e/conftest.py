"""
TEST-04 end-to-end harness: a real ARIA server (FastAPI + Gradio) on a
throwaway `aria_e2e` database, with the deterministic LLM (ARIA_FAKE_LLM)
and offline market data (ARIA_FAKE_DATA), driven by Playwright.

Run:  E2E=1 TEST_DATABASE_URL=postgresql://aria:aria@localhost:55432/aria_test \\
      python -m pytest tests/e2e -p no:cacheprovider
      (first time: python -m playwright install chromium)

Or against a running stack:  docker compose -f compose.test.yml up --build -d
      E2E=1 E2E_BASE_URL=http://localhost:7870       E2E_DATABASE_URL=postgresql://aria:aria@localhost:55433/aria_e2e python -m pytest tests/e2e

Skipped unless E2E=1. On failure a Playwright trace is kept under
test-results/ (CI uploads it as an artifact).
"""

from __future__ import annotations

import os
import re
import socket
import subprocess  # nosec B404 - starts this repo's own server
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "e2e-password-123"


def pytest_collection_modifyitems(config, items):
    if os.getenv("E2E") == "1":
        return
    skip = pytest.mark.skip(reason="end-to-end UI tests: set E2E=1 (needs Postgres and Playwright browsers)")
    for item in items:
        if "e2e" in Path(str(item.fspath)).parts:
            item.add_marker(skip)


def _e2e_url() -> str:
    base = os.getenv("TEST_DATABASE_URL", "").strip()
    if not base:
        pytest.skip("TEST_DATABASE_URL is needed for the e2e database")
    return re.sub(r"/[^/?]+(\?|$)", r"/aria_e2e\1", base, count=1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def e2e_db() -> str:
    import psycopg

    if os.getenv("E2E_DATABASE_URL"):  # the database behind E2E_BASE_URL, already seeded
        return os.environ["E2E_DATABASE_URL"]
    url = _e2e_url()
    admin = re.sub(r"/aria_e2e(\?|$)", r"/postgres\1", url)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'aria_e2e' AND pid <> pg_backend_pid()"
        )
        conn.execute("DROP DATABASE IF EXISTS aria_e2e")
        conn.execute("CREATE DATABASE aria_e2e")
    env = {**os.environ, "ARIA_NO_DOTENV": "1", "DATABASE_URL": url, "APP_ENV": "test", "ARIA_FAKE_DATA": "1"}
    subprocess.run([sys.executable, str(ROOT / "tests" / "e2e" / "seed.py")], env=env, check=True, cwd=ROOT)  # noqa: S603
    return url


def server_env(db_url: str, port: int, **extra: str) -> dict[str, str]:
    return {
        **{
            k: v
            for k, v in os.environ.items()
            if not k.startswith(
                ("GROQ_", "GEMINI_", "LOCAL_LLM", "LANGSMITH", "LANGCHAIN", "SMTP_", "TELEGRAM_")
            )
        },
        "ARIA_NO_DOTENV": "1",  # never the developer's real .env
        "DATABASE_URL": db_url,
        "APP_ENV": "dev",
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "SECRET_KEY": "e2e-secret-key-0123456789-abcdefghijklmnop",
        "ARIA_FAKE_LLM": "1",
        "ARIA_FAKE_DATA": "1",
        "RUN_JOBS_IN_APP": "false",
        "TELEGRAM_BOT_TOKEN": "123456:e2e-dummy-token",  # no poller runs (RUN_JOBS_IN_APP=false)
        "TELEGRAM_BOT_USERNAME": "aria_e2e_bot",
        "MAIL_BACKEND": "console",
        "REQUIRE_EMAIL_VERIFICATION": "false",
        "TRIAL_DAILY_LIMIT": "5",
        "TURNS_PER_MINUTE": "60",
        "AUTH_IP_MAX_ATTEMPTS": "1000",  # every journey signs up from 127.0.0.1
        "PYTHONIOENCODING": "utf-8",
        **extra,
    }


def start_server(env: dict[str, str], log_path: Path) -> subprocess.Popen:
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "app.py"], env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT
    )  # noqa: S603
    base = f"http://127.0.0.1:{env['PORT']}"
    import urllib.request

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early — see {log_path}")
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=2) as r:  # noqa: S310  # nosec B310
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"server didn't come up — see {log_path}")


@pytest.fixture(scope="session")
def base_url(e2e_db, tmp_path_factory) -> Iterator[str]:
    if os.getenv("E2E_BASE_URL"):  # an already running stack (compose.test.yml)
        yield os.environ["E2E_BASE_URL"].rstrip("/")
        return
    port = _free_port()
    proc = start_server(server_env(e2e_db, port), tmp_path_factory.mktemp("server") / "server.log")
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def db(e2e_db):
    """Direct reads of the e2e database for assertions."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(e2e_db, row_factory=dict_row, autocommit=True) as conn:
        yield conn


_counter = iter(range(1, 10_000))


@pytest.fixture
def email() -> str:
    return f"user{next(_counter)}-{int(time.time() * 1000) % 10_000_000}@e2e.example.test"
