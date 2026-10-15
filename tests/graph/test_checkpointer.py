from unittest.mock import MagicMock, patch

# Import before any test patches psycopg_pool.ConnectionPool: the module binds
# ConnectionPool at import time, and importing it under that patch would leave
# a mock in it for the rest of the session (breaking the real saver later).
import langgraph.checkpoint.postgres  # noqa: F401

import graph.checkpointer as checkpointer_module


def _reset_singleton():
    checkpointer_module._checkpointer = None
    checkpointer_module._pool = None


class TestGetPostgresCheckpointer:
    def setup_method(self):
        _reset_singleton()

    def teardown_method(self):
        _reset_singleton()

    def test_never_calls_setup(self):
        """ARCH-01: checkpoint tables are created by scripts/migrate.py, not by
        the app process."""
        fake_saver = MagicMock()
        with (
            patch("psycopg_pool.ConnectionPool"),
            patch("langgraph.checkpoint.postgres.PostgresSaver", return_value=fake_saver),
        ):
            checkpointer_module.get_postgres_checkpointer()
        fake_saver.setup.assert_not_called()

    def test_returns_singleton_on_repeated_calls(self):
        fake_saver = MagicMock()
        with (
            patch("psycopg_pool.ConnectionPool"),
            patch("langgraph.checkpoint.postgres.PostgresSaver", return_value=fake_saver),
        ):
            first = checkpointer_module.get_postgres_checkpointer()
            second = checkpointer_module.get_postgres_checkpointer()
        assert first is second
