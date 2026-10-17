"""Real-Postgres fixtures for integration tests (SEC-13, DATA-01).

Database source, in order:
1. TEST_DATABASE_URL (CI sets this to a Postgres service container), or
2. a throwaway testcontainers Postgres if Docker is reachable, else
3. the tests are skipped with the reason.

Safety: the app's own DATABASE_URL (from .env — possibly production) is never
used; the fixture refuses to run if TEST_DATABASE_URL points at it.
"""

import os

import pytest


@pytest.fixture(scope="session")
def pg_url():
    url = os.getenv("TEST_DATABASE_URL", "").strip()
    container = None
    if not url:
        try:
            import docker

            try:
                from testcontainers.community.postgres import PostgresContainer
            except ImportError:
                from testcontainers.postgres import PostgresContainer

            docker.from_env().ping()
        except Exception as e:
            pytest.skip(
                f"no TEST_DATABASE_URL and Docker isn't reachable ({type(e).__name__}) — skipping integration tests"
            )
        container = PostgresContainer("postgres:16-alpine", driver=None)
        container.start()
        url = container.get_connection_url()
    import config

    if config.DATABASE_URL and url.strip() == config.DATABASE_URL.strip():
        pytest.fail("TEST_DATABASE_URL equals the app's DATABASE_URL — refusing to run tests against it")
    yield url
    if container is not None:
        container.stop()


@pytest.fixture(scope="session")
def pg_db(pg_url):
    """utils.db pointed at the test database, schema migrated to head the
    same way a deploy does it (ARCH-01: scripts/migrate.py)."""
    import config
    import utils.db as db
    from utils import migrations

    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DATABASE_URL", pg_url)
    mp.setattr(db, "DATABASE_URL", pg_url)
    saved_pool = db._pool
    db._pool = None
    migrations.upgrade(pg_url)
    migrations.setup_checkpointer_tables(pg_url)
    yield db
    db.close_pool()
    db._pool = saved_pool
    mp.undo()


@pytest.fixture
def db(pg_db):
    """Empty tables for every test."""
    tables = pg_db._query(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' "
        "AND tablename NOT LIKE 'checkpoint%%' AND tablename NOT IN ('alembic_version', 'terms_versions')"
    )
    names = ", ".join(f'"{t["tablename"]}"' for t in tables)
    if names:
        pg_db._query(f"TRUNCATE {names} RESTART IDENTITY CASCADE", fetch=None, commit=True)
    return pg_db


@pytest.fixture
def two_users(db):
    a = db._query(
        "INSERT INTO users (email, password_hash) VALUES ('a@example.com','x') RETURNING id",
        fetch="one",
        commit=True,
    )["id"]
    b = db._query(
        "INSERT INTO users (email, password_hash) VALUES ('b@example.com','x') RETURNING id",
        fetch="one",
        commit=True,
    )["id"]
    return a, b
