"""REL-04: Neon's -pooler endpoint refuses `statement_timeout` as a startup
option ("unsupported startup parameter in options"), so it is only sent to
hosts that accept it."""

import pytest

from utils import db


@pytest.mark.parametrize(
    ("url", "sends_option"),
    [
        ("postgresql://u:p@ep-x-123-pooler.c-10.us-east-1.aws.neon.tech/neondb?sslmode=require", False),
        ("postgresql://u:p@ep-x-123.c-10.us-east-1.aws.neon.tech/neondb?sslmode=require", True),
        ("postgresql://aria:aria@localhost:5432/aria", True),
    ],
)
def test_statement_timeout_option_skipped_only_on_neon_pooler(monkeypatch, url, sends_option):
    monkeypatch.setattr(db, "DATABASE_URL", url)
    monkeypatch.setattr(db, "DB_STATEMENT_TIMEOUT_MS", 5000)
    assert ("options" in db.connection_kwargs()) is sends_option


def test_zero_timeout_sends_no_option(monkeypatch):
    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://aria:aria@localhost:5432/aria")
    monkeypatch.setattr(db, "DB_STATEMENT_TIMEOUT_MS", 0)
    assert "options" not in db.connection_kwargs()
