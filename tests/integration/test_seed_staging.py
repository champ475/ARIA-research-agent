"""OPS-01 on a real Postgres: the staging seed is synthetic, idempotent and
refuses production."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_seed_creates_pii_free_users_with_data_and_is_idempotent(db):
    import scripts.seed_staging as seed

    assert seed.main(["--users", "3"]) == 0
    users = db._query("SELECT id, email, email_verified_at, accepted_terms_version FROM users ORDER BY id")
    assert [u["email"] for u in users] == [f"demo{i}@staging.example.test" for i in (1, 2, 3)]
    assert all(u["email_verified_at"] and u["accepted_terms_version"] for u in users)
    uid = users[0]["id"]
    assert len(db.watchlist_all(uid)) == 4 and db.portfolio_all(uid) and db.alert_list(uid)
    assert seed.main(["--users", "3"]) == 0
    assert db._query("SELECT count(*) AS n FROM users", fetch="one")["n"] == 3


def test_seed_refuses_production(db, monkeypatch):
    import config
    import scripts.seed_staging as seed

    monkeypatch.setattr(
        config, "get_settings", lambda: config.Settings(APP_ENV="prod", DATABASE_URL=db.DATABASE_URL)
    )
    with pytest.raises(SystemExit, match="APP_ENV=prod"):
        seed.main(["--users", "1"])
    host = config.db_host(db.DATABASE_URL)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(APP_ENV="staging", DATABASE_URL=db.DATABASE_URL, PROD_DATABASE_HOSTS=host),
    )
    with pytest.raises(SystemExit, match="production database"):
        seed.main(["--users", "1"])
    assert db._query("SELECT count(*) AS n FROM users", fetch="one")["n"] == 0
