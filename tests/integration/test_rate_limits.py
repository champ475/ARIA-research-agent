"""SEC-12 on a real Postgres: the trial quota is atomic under concurrency and
every user is limited to TURNS_PER_MINUTE turns."""

from __future__ import annotations

import threading
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.integration


def test_twenty_concurrent_requests_at_used_4_allow_exactly_one(db, two_users):
    a, _ = two_users
    today = date(2026, 10, 15)
    db._query(
        "UPDATE users SET free_msgs_used_today=4, free_msgs_reset_at=%s WHERE id=%s",
        (today, a),
        fetch=None,
        commit=True,
    )
    results: list[bool] = []
    barrier = threading.Barrier(20)

    def take():
        barrier.wait(10)
        results.append(db.trial_quota_check_and_increment(a, 5, today))

    threads = [threading.Thread(target=take) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results.count(True) == 1 and len(results) == 20


def test_rate_limit_is_exact_under_a_burst(db):
    allowed: list[bool] = []
    barrier = threading.Barrier(30)

    def take():
        barrier.wait(10)
        allowed.append(db.rate_limit_take("turn:burst", 10, 60))

    threads = [threading.Thread(target=take) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert allowed.count(True) == 10


def test_eleventh_turn_in_a_minute_gets_a_clear_refusal(db, two_users, app_module):
    a, _ = two_users
    orch = MagicMock()
    orch.classify.return_value = "chat"
    orch.process_stream.side_effect = lambda _m, progress=False: iter(
        [("answer", None, None, None, None, "chat")]
    )
    orch.generate_suggestions.return_value = []
    with (
        patch.object(app_module, "get_orchestrator", return_value=orch),
        patch.object(app_module, "_persist_turn"),
    ):
        replies = [list(app_module.respond_exclusive(a, f"q{i}", [], [], 1, "1M"))[-1] for i in range(11)]
    assert all("answer" in r[2][-1]["content"] for r in replies[:10])
    assert "faster than ARIA can answer" in replies[10][2][-1]["content"]
    assert orch.process_stream.call_count == 10
