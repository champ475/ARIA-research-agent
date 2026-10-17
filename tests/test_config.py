"""ARCH-03: typed, validated configuration and shared helpers defined once."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import config

ROOT = Path(__file__).resolve().parents[1]
APP_DIRS = ("agents", "graph", "tools", "utils", "scripts", "services", "ui", "web")


_NO_LLM = {f"GROQ_API_KEY_{i}": "" for i in range(1, 6)} | {"GEMINI_API_KEY": "", "LOCAL_LLM_BASE_URL": ""}


def _run(code: str, **env: str) -> subprocess.CompletedProcess[str]:
    full_env = {k: v for k, v in os.environ.items() if k not in env}
    full_env.update(env)
    return subprocess.run(  # noqa: S603  # nosec B603 - fixed interpreter + literal code
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_bad_typed_value_exits_with_one_readable_message():
    r = _run("import config", LLM_TIMEOUT_SECONDS="abc", DB_POOL_MAX="0")
    assert r.returncode == 2
    assert "ARIA configuration error" in r.stderr
    assert "LLM_TIMEOUT_SECONDS" in r.stderr and "DB_POOL_MAX" in r.stderr
    assert "Traceback" not in r.stderr


def test_boot_with_bad_database_url_exits_non_zero():
    r = _run("import config; config.check_boot()", DATABASE_URL="mysql://u:p@h/db")
    assert r.returncode == 2
    assert "ARIA cannot start" in r.stderr and "postgresql://" in r.stderr
    assert "Traceback" not in r.stderr


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("", False),
        ("mysql://u:p@h/db", False),
        ("postgresql://nohost", False),
        ("postgresql://u:p@host/db", True),
        ("postgres://u:p@host:5432/db?sslmode=require", True),
    ],
)
def test_database_url_shape(url, ok):
    s = config.Settings(DATABASE_URL=url, APP_ENV="dev")
    problems = [p for p in config.boot_problems(s) if "DATABASE_URL" in p]
    assert (not problems) is ok


def test_prod_needs_an_llm_provider_but_worker_does_not():
    s = config.Settings(
        DATABASE_URL="postgresql://u:p@h/db",
        APP_ENV="prod",
        **_NO_LLM,
    )
    assert any("LLM provider" in p for p in config.boot_problems(s))
    assert not any("LLM provider" in p for p in config.boot_problems(s, serving=False))


def test_normalisation_and_derived_values():
    s = config.Settings(
        **(_NO_LLM | {"PREFERRED_PROVIDER": " Gemini ", "LOG_LEVEL": "debug", "GROQ_API_KEY_2": "k2"})
    )
    assert s.PREFERRED_PROVIDER == "gemini"
    assert s.LOG_LEVEL == "DEBUG"
    assert s.groq_api_keys == ["k2"]
    assert config.Settings(LOCAL_LLM_BASE_URL="").turn_deadline_seconds == 90.0
    assert config.Settings(LOCAL_LLM_BASE_URL="http://x/v1").turn_deadline_seconds == 600.0
    assert config.Settings(TURN_DEADLINE_SECONDS=45, LOCAL_LLM_BASE_URL="").turn_deadline_seconds == 45.0


def _sources():
    for d in APP_DIRS:
        yield from (ROOT / d).rglob("*.py")
    yield ROOT / "app.py"


@pytest.mark.parametrize(
    "pattern",
    [
        r"^def _strip_code_fence\(",
        r"^_SUMMARY_PROMPT = ",
        r"^_CANONICAL_RATIO_KEYS = ",
        r"^_?(FREE_)?TRIAL_DAILY_LIMIT = ",
    ],
)
def test_shared_symbols_are_defined_once(pattern):
    """These used to be copy-pasted and drifted (ARCH-03)."""
    hits = [str(p) for p in _sources() if re.search(pattern, p.read_text(encoding="utf-8"), re.M)]
    assert hits == [], f"duplicate definition of {pattern}: {hits}"


_STATIC_SETTINGS = (
    "DB_POOL_MAX",
    "DB_POOL_ACQUIRE_TIMEOUT",
    "BREAKER_FAILURES",
    "IO_POOL_WORKERS",
    "TOOL_POOL_WORKERS",
    "CHAT_CONCURRENCY",
    "QUEUE_MAX_SIZE",
    "ALERT_MAX_DELIVERY_ATTEMPTS",
)


def test_static_settings_are_read_only_through_config():
    pat = re.compile(r"os\.(getenv|environ\.get)\(\s*[\"'](" + "|".join(_STATIC_SETTINGS) + r")[\"']")
    hits = [str(p) for p in _sources() if pat.search(p.read_text(encoding="utf-8"))]
    assert hits == []


# ── OPS-01: no production database outside prod ──────────────────────────────


def test_non_prod_refuses_the_production_database():
    from config import Settings, boot_problems, prod_db_problem

    prod = "ep-prod-1-pooler.ap-south-1.aws.neon.tech"
    url = f"postgresql://u:p@{prod}/neondb?sslmode=require"
    dev = Settings(APP_ENV="dev", DATABASE_URL=url, PROD_DATABASE_HOSTS=f"{prod.upper()}, other.host")
    assert "must not use the production database" in (prod_db_problem(dev) or "")
    assert any("production database" in p for p in boot_problems(dev, serving=False))
    jobs = Settings(
        APP_ENV="staging",
        DATABASE_URL="postgresql://u:p@localhost/aria",
        JOBS_DATABASE_URL=url,
        PROD_DATABASE_HOSTS=prod,
    )
    assert "JOBS_DATABASE_URL" in (prod_db_problem(jobs) or "")
    assert prod_db_problem(Settings(APP_ENV="prod", DATABASE_URL=url, PROD_DATABASE_HOSTS=prod)) is None
    assert prod_db_problem(Settings(APP_ENV="dev", DATABASE_URL=url)) is None  # guard not configured
    assert (
        prod_db_problem(
            Settings(APP_ENV="dev", DATABASE_URL="postgresql://u:p@localhost/x", PROD_DATABASE_HOSTS=prod)
        )
        is None
    )


def test_empty_optional_values_mean_unset():
    # `REQUIRE_EMAIL_VERIFICATION=   # comment` in .env reaches pydantic as "".
    s = config.Settings(
        APP_ENV="dev",
        RUN_JOBS_IN_APP="",
        COOKIE_SECURE=" ",
        REQUIRE_EMAIL_VERIFICATION="",
        TURN_DEADLINE_SECONDS="",
    )
    assert (s.RUN_JOBS_IN_APP, s.COOKIE_SECURE, s.REQUIRE_EMAIL_VERIFICATION, s.TURN_DEADLINE_SECONDS) == (
        None,
        None,
        None,
        None,
    )
    assert (
        s.run_jobs_in_app is True
        and s.require_email_verification is False
        and s.turn_deadline_seconds == 90.0
    )
