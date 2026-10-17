"""Alembic environment (ARCH-01).

Migrations are plain SQL (op.execute) — there are no SQLAlchemy models; the
app talks to Postgres through psycopg 3 (utils/db.py). SQLAlchemy is only
Alembic's connection layer here.

Database URL, first match wins:
1. config.attributes["url"] (scripts/migrate.py, tests/integration/conftest.py)
2. `alembic -x url=postgresql://...`
3. DATABASE_URL from the environment / .env (config.py)
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

# The alembic CLI gets its plain-text logging from alembic.ini; when called
# from Python (scripts/migrate.py, tests) the app's JSON logging is already
# configured and must not be replaced.
if config.config_file_name and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)


def _url() -> str:
    url = config.attributes.get("url") or context.get_x_argument(as_dictionary=True).get("url")
    if not url:
        from config import DATABASE_URL

        url = DATABASE_URL
    if not url:
        raise RuntimeError("No database URL: set DATABASE_URL or pass -x url=...")
    # psycopg (v3) is the app's one Postgres driver (REL-04).
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # One transaction per revision: a failing revision rolls back alone and
        # leaves alembic_version at the last good one.
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
