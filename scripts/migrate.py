"""
scripts/migrate.py — the release step (ARCH-01): bring the database schema to
the Alembic head and create LangGraph's checkpoint tables. Run once per
deploy, before starting the app. Idempotent: running it on an up-to-date
database does nothing.

Usage:
    python scripts/migrate.py                 # DATABASE_URL from env / .env
    python scripts/migrate.py --url URL       # explicit target
    python scripts/migrate.py --check         # exit 1 if not at head, change nothing
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger("migrate")


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="database URL (default: DATABASE_URL)")
    parser.add_argument("--check", action="store_true", help="only report whether the schema is at head")
    args = parser.parse_args(argv)

    import psycopg2

    from config import DATABASE_URL
    from utils import migrations

    url = args.url or DATABASE_URL
    if not url:
        log.error("No database URL: set DATABASE_URL or pass --url")
        return 2

    if args.check:
        conn = psycopg2.connect(url)
        try:
            migrations.check_schema(conn)
        except migrations.SchemaOutOfDate as e:
            log.error(str(e))
            return 1
        finally:
            conn.close()
        log.info(f"Schema is at head ({migrations.head_revision()}).")
        return 0

    log.info(f"Upgrading schema to {migrations.head_revision()} ...")
    migrations.upgrade(url)
    migrations.setup_checkpointer_tables(url)
    log.info("Schema is at head; checkpoint tables ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
