"""REL-08 on a real Postgres: the drill's checks pass on a healthy database
and catch a SECRET_KEY that can't read the stored BYO keys."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_checks_pass_and_catch_a_lost_secret_key(db, two_users, monkeypatch):
    from scripts import restore_drill
    from utils import crypto

    a, _ = two_users
    db.llm_config_set(a, "openai_compatible", "gpt-x", crypto.encrypt("sk-test"))
    ok = restore_drill.checks(db.DATABASE_URL)
    assert ok["schema"] == "at the latest migration" and ok["problems"] == []
    assert ok["counts"]["users"] == 2 and ok["byo_keys"] == {"checked": 1, "unreadable": 0}
    assert restore_drill.app_ready(db.DATABASE_URL) is None

    monkeypatch.setattr("config.SECRET_KEY", "a-completely-different-secret-key-0123456789")
    monkeypatch.setattr("config.SECRET_KEY_PREVIOUS", "")
    crypto.reset_cache()
    try:
        lost = restore_drill.checks(db.DATABASE_URL)
    finally:
        monkeypatch.undo()
        crypto.reset_cache()
    assert lost["byo_keys"]["unreadable"] == 1 and "don't decrypt" in lost["problems"][0]
