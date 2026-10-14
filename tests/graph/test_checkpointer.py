from unittest.mock import MagicMock, patch

import graph.checkpointer as checkpointer_module


def _reset_singleton():
    checkpointer_module._checkpointer = None
    checkpointer_module._pool = None


class TestGetPostgresCheckpointer:
    def setup_method(self):
        _reset_singleton()

    def teardown_method(self):
        _reset_singleton()

    def test_calls_setup_exactly_once(self):
        fake_saver = MagicMock()
        with (
            patch("psycopg_pool.ConnectionPool"),
            patch("langgraph.checkpoint.postgres.PostgresSaver", return_value=fake_saver),
        ):
            checkpointer_module.get_postgres_checkpointer()
        fake_saver.setup.assert_called_once()

    def test_returns_singleton_on_repeated_calls(self):
        fake_saver = MagicMock()
        with (
            patch("psycopg_pool.ConnectionPool"),
            patch("langgraph.checkpoint.postgres.PostgresSaver", return_value=fake_saver),
        ):
            first = checkpointer_module.get_postgres_checkpointer()
            second = checkpointer_module.get_postgres_checkpointer()
        assert first is second
        fake_saver.setup.assert_called_once()  # not called again on second access
