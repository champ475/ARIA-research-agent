"""REL-01 — chat turns run concurrently across users, one at a time per user,
and the DB pool makes callers wait instead of failing when it's full."""

import threading
import time
from unittest.mock import MagicMock, patch


def _fake_orch(on_stream=None):
    orch = MagicMock()
    orch.classify.return_value = "chat"

    def stream(_msg):
        if on_stream:
            on_stream()
        yield ("answer", None, None, None, None, "chat")

    orch.process_stream.side_effect = stream
    orch.generate_suggestions.return_value = []
    return orch


def test_queue_is_configured_for_concurrent_turns(app_module):
    kwargs = app_module._queue_kwargs()
    assert kwargs["default_concurrency_limit"] == app_module.CHAT_CONCURRENCY >= 2
    assert kwargs["max_size"] >= 1


def test_chat_events_share_one_concurrent_pool(app_module):
    demo = app_module.build_ui()
    chat_fns = [fn for fn in demo.fns.values() if getattr(fn, "concurrency_id", None) == "chat"]
    assert len(chat_fns) == 4  # Enter, the Send button, and the Yes/No confirmation buttons
    assert all(fn.concurrency_limit == app_module.CHAT_CONCURRENCY for fn in chat_fns)


def test_second_turn_from_same_user_gets_busy_reply(app_module):
    started, release = threading.Event(), threading.Event()
    orch = _fake_orch(on_stream=lambda: (started.set(), release.wait(5)))
    with (
        patch.object(app_module, "get_orchestrator", return_value=orch),
        patch.object(app_module, "_persist_turn"),
    ):
        first = app_module.respond_exclusive(101, "TCS price", [], [], 1, "1M")
        runner = threading.Thread(target=lambda: list(first))
        runner.start()
        assert started.wait(5)
        second = list(app_module.respond_exclusive(101, "INFY price", [], [], 1, "1M"))
        release.set()
        runner.join(5)
    (busy,) = second
    assert "Still working" in busy[2][-1]["content"]
    assert orch.process_stream.call_count == 1


def test_lock_is_released_after_turn_and_after_cancel(app_module):
    with (
        patch.object(app_module, "get_orchestrator", return_value=_fake_orch()),
        patch.object(app_module, "_persist_turn"),
    ):
        list(app_module.respond_exclusive(102, "a", [], [], 1, "1M"))
        assert not app_module._user_turn_lock(102).locked()
        gen = app_module.respond_exclusive(102, "b", [], [], 1, "1M")
        next(gen)  # loading frame — lock held
        assert app_module._user_turn_lock(102).locked()
        gen.close()  # client disconnected
        assert not app_module._user_turn_lock(102).locked()


def test_different_users_run_in_parallel(app_module):
    barrier = threading.Barrier(2, timeout=5)  # both turns must be inside process_stream at once
    orch = _fake_orch(on_stream=barrier.wait)
    errors = []

    def run(uid):
        try:
            list(app_module.respond_exclusive(uid, "TCS", [], [], 1, "1M"))
        except Exception as e:  # BrokenBarrierError if turns were serialised
            errors.append(e)

    with (
        patch.object(app_module, "get_orchestrator", return_value=orch),
        patch.object(app_module, "_persist_turn"),
    ):
        threads = [threading.Thread(target=run, args=(uid,)) for uid in (201, 202)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert errors == []
    assert orch.process_stream.call_count == 2


def test_get_orchestrator_builds_one_instance_under_contention(app_module):
    built = []

    class _Slow:
        def __init__(self, uid):
            time.sleep(0.05)
            built.append(uid)

    app_module._orchestrators.pop(303, None)
    with patch("agents.graph_orchestrator.GraphOrchestrator", _Slow):
        threads = [threading.Thread(target=app_module.get_orchestrator, args=(303,)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
    app_module._orchestrators.pop(303, None)
    assert built == [303]


# Pool waiting / timeout / discard behaviour runs against a real Postgres:
# tests/integration/test_db_pool.py (REL-04).
