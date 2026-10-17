"""OPS-04: in-flight turn tracking, draining, and the shutdown wait."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from utils import lifecycle


@pytest.fixture(autouse=True)
def _fresh_lifecycle():
    lifecycle.reset()
    yield
    lifecycle.reset()


def test_wait_idle_returns_once_running_turns_finish():
    started = threading.Event()

    def turn():
        with lifecycle.track_turn():
            started.set()
            time.sleep(0.3)

    t = threading.Thread(target=turn)
    t.start()
    assert started.wait(2)
    assert lifecycle.in_flight() == 1
    t0 = time.monotonic()
    assert lifecycle.wait_idle(timeout=5) is True
    assert 0.1 < time.monotonic() - t0 < 3
    assert lifecycle.in_flight() == 0


def test_wait_idle_gives_up_after_the_grace_period():
    release = threading.Event()

    def turn():
        with lifecycle.track_turn():
            release.wait(5)

    t = threading.Thread(target=turn)
    t.start()
    while lifecycle.in_flight() == 0:
        time.sleep(0.01)
    assert lifecycle.wait_idle(timeout=0.2) is False
    release.set()
    t.join(5)


def test_turn_counter_survives_exceptions():
    with pytest.raises(RuntimeError), lifecycle.track_turn():
        raise RuntimeError("boom")
    assert lifecycle.in_flight() == 0


def _fake_orch(delay: float = 0.0):
    orch = MagicMock()
    orch.classify.return_value = "chat"

    def stream(_msg, progress=False):
        time.sleep(delay)
        yield ("full answer text", None, None, None, None, "chat")

    orch.process_stream.side_effect = stream
    orch.generate_suggestions.return_value = []
    return orch


def test_draining_server_refuses_new_turns_politely(app_module):
    lifecycle.begin_drain()
    orch = _fake_orch()
    with patch.object(app_module, "get_orchestrator", return_value=orch):
        (only,) = list(app_module.respond_exclusive(501, "TCS price", [], [], 1, "1M"))
    assert "restarting" in only[2][-1]["content"]
    assert orch.process_stream.call_count == 0


def test_a_running_turn_finishes_during_drain(app_module):
    """The rolling-deploy guarantee: a turn already streaming when SIGTERM
    arrives completes, and shutdown waits for it."""
    orch = _fake_orch(delay=0.3)
    frames: list = []
    with (
        patch.object(app_module, "get_orchestrator", return_value=orch),
        patch.object(app_module, "_persist_turn"),
    ):
        t = threading.Thread(
            target=lambda: frames.extend(app_module.respond_exclusive(502, "q", [], [], 1, "1M"))
        )
        t.start()
        while lifecycle.in_flight() == 0:
            time.sleep(0.01)
        lifecycle.begin_drain()
        assert lifecycle.wait_idle(timeout=5) is True
        t.join(5)
    assert "full answer text" in frames[-1][2][-1]["content"]
