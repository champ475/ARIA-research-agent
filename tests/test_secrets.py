"""SEC-11: weak secrets refused at boot, key rotation, tokens never logged."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import requests

import config
import utils.crypto as crypto
from utils import logging_setup as ls

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "1234567890:AAEexampleBotTokenValue_abcdefghijklmnop"
GOOD = "x" * 40


@pytest.mark.parametrize(
    ("secret", "fragment"),
    [
        ("", "not set"),
        (config.SECRET_KEY_PLACEHOLDER, "placeholder"),
        ("short-secret", "at least 32"),
    ],
)
def test_weak_secret_key_problems(secret, fragment):
    problems = config.secret_key_problems(secret)
    assert len(problems) == 1 and fragment in problems[0]


def test_strong_secret_key_passes():
    assert config.secret_key_problems(GOOD) == []


def _boot(secret: str, app_env: str = "dev") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(SECRET_KEY=secret, APP_ENV=app_env, DATABASE_URL="postgresql://u:p@h/db")
    return subprocess.run(  # noqa: S603  # nosec B603 - fixed interpreter + literal code
        [sys.executable, "-c", "import config; config.check_boot()"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_boot_fails_on_the_placeholder():
    r = _boot(config.SECRET_KEY_PLACEHOLDER)
    assert r.returncode == 2
    assert "placeholder" in r.stderr
    assert config.SECRET_KEY_PLACEHOLDER not in r.stderr.replace("placeholder from", "")


def test_boot_passes_with_a_strong_key_and_test_env_is_exempt():
    assert _boot(GOOD).returncode == 0
    assert _boot("", app_env="test").returncode == 0


# ── rotation ──────────────────────────────────────────────────────────────────


@pytest.fixture
def keys(monkeypatch):
    def use(secret: str, previous: str = "") -> None:
        monkeypatch.setattr(config, "SECRET_KEY", secret)
        monkeypatch.setattr(config, "SECRET_KEY_PREVIOUS", previous)
        crypto.reset_cache()

    yield use
    crypto.reset_cache()


def test_old_ciphertext_still_decrypts_during_the_rotation_window(keys):
    keys("old-" + GOOD)
    old_ct = crypto.encrypt("sk-user-key")
    keys("new-" + GOOD, previous="old-" + GOOD)
    assert crypto.decrypt(old_ct) == "sk-user-key"
    new_ct = crypto.rotate(old_ct)
    assert new_ct != old_ct
    keys("new-" + GOOD)  # window closed: only the new key remains
    assert crypto.decrypt(new_ct) == "sk-user-key"
    with pytest.raises(ValueError):
        crypto.decrypt(old_ct)


def test_rotate_rejects_unreadable_ciphertext(keys):
    keys("a-" + GOOD)
    ct = crypto.encrypt("k")
    keys("b-" + GOOD)
    with pytest.raises(ValueError, match="not readable"):
        crypto.rotate(ct)


def test_rotate_all_rewraps_rows(keys, monkeypatch):
    import scripts.rotate_secret_key as rot

    keys("old-" + GOOD)
    rows = [
        {"user_id": 1, "api_key_encrypted": crypto.encrypt("k1")},
        {"user_id": 2, "api_key_encrypted": "junk"},
    ]
    written: dict[int, str] = {}
    monkeypatch.setattr("utils.db.llm_config_ciphertexts", lambda: rows)
    monkeypatch.setattr(
        "utils.db.llm_config_rewrap", lambda uid, old, new: written.setdefault(uid, new) is not None
    )
    keys("new-" + GOOD, previous="old-" + GOOD)
    assert rot.rotate_all() == {"rows": 2, "rotated": 1, "unreadable": 1, "changed_meanwhile": 0}
    keys("new-" + GOOD)
    assert crypto.decrypt(written[1]) == "k1"


# ── log redaction ─────────────────────────────────────────────────────────────


@pytest.fixture
def captured():
    buf = io.StringIO()
    ls.configure_logging(level="DEBUG", stream=buf)
    yield buf
    ls._configured = False


def test_forced_telegram_exception_never_logs_the_token(captured, monkeypatch):
    from utils import telegram_link

    monkeypatch.setattr(telegram_link, "TELEGRAM_BOT_TOKEN", TOKEN)

    def boom(url, **kwargs):
        raise requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")

    monkeypatch.setattr(telegram_link.requests, "post", boom)
    telegram_link._send(42, "hi")
    out = captured.getvalue()
    assert "[telegram_link] confirmation send failed" in out
    assert TOKEN not in out and TOKEN.split(":")[1] not in out
    assert "bot***" in out


def test_filter_redacts_args_and_exception_text(captured):
    log = logging.getLogger("tests.secrets")
    try:
        raise RuntimeError(f"call to https://api.telegram.org/bot{TOKEN}/getUpdates failed")
    except RuntimeError:
        log.exception("failed with key %s", "gsk_abcdefghijklmnopqrstuv")
    line = json.loads(captured.getvalue().splitlines()[-1])
    assert "gsk_***" in line["msg"] and "abcdefghijklmnop" not in line["msg"]
    assert TOKEN not in line["exc"] and "bot***" in line["exc"]


def test_filter_survives_a_bad_format_string(captured):
    logging.getLogger("tests.secrets").warning("broken %s %s", "only-one")
    assert "broken" in captured.getvalue()
