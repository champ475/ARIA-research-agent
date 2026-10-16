"""REL-05: when each background job is due, and the scheduler's dispatch."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from datetime import time as dtime
from unittest.mock import patch

import worker
from utils.helpers import IST
from utils.jobs import Job, is_due


def at(y, m, d, hh, mm=0):
    return IST.localize(datetime(y, m, d, hh, mm))


def _noop():
    return None


def test_interval_jobs():
    job = Job("x", _noop, every=timedelta(minutes=5))
    now = at(2026, 10, 15, 11)
    assert is_due(job, None, now)
    assert not is_due(job, now - timedelta(minutes=4), now)
    assert is_due(job, now - timedelta(minutes=5), now)


def test_daily_jobs_run_once_after_their_slot():
    job = Job("x", _noop, daily_at=dtime(19, 30))
    yesterday = at(2026, 10, 14, 19, 31)
    assert not is_due(job, yesterday, at(2026, 10, 15, 19, 0))  # before today's slot
    assert is_due(job, yesterday, at(2026, 10, 15, 19, 30))
    assert not is_due(job, at(2026, 10, 15, 19, 31), at(2026, 10, 15, 23, 0))  # already ran today
    assert not is_due(job, None, at(2026, 10, 15, 9, 0))  # first start: still waits for the slot
    assert is_due(job, None, at(2026, 10, 15, 20, 0))


def test_gates_on_the_market_calendar():
    by = {j.name: j for j in worker.JOBS}
    alerts, snapshot = by["alert_check"], by["fundamentals_snapshot"]
    assert is_due(alerts, None, at(2026, 10, 15, 11))  # Thursday, open
    assert not is_due(alerts, None, at(2026, 10, 15, 16))  # after the close
    assert not is_due(alerts, None, at(2026, 10, 20, 11))  # Dussehra
    assert is_due(snapshot, None, at(2026, 10, 15, 20))
    assert not is_due(snapshot, None, at(2026, 10, 18, 20))  # Sunday


def test_scheduler_submits_due_jobs_once():
    gate = threading.Event()
    ran: list[str] = []

    def fake_run(job, *a, **k):
        ran.append(job.name)
        gate.wait(5)
        return "ran"

    jobs = [Job("a", _noop, every=timedelta(minutes=5)), Job("b", _noop, every=timedelta(hours=1))]
    now = at(2026, 10, 15, 11)
    beats = [{"job": "b", "last_started_at": now - timedelta(minutes=10)}]
    sched = worker.Scheduler(jobs)
    with patch("utils.db.job_heartbeats", return_value=beats), patch.object(worker, "run_job", fake_run):
        assert sched.tick(now) == ["a"]  # b ran 10 min ago
        assert sched.tick(now) == []  # a is still running here: not submitted twice
        gate.set()
        sched._pool.shutdown(wait=True)
    assert ran == ["a"]


def test_app_runs_jobs_in_process_only_when_configured():
    from config import Settings

    assert Settings(APP_ENV="dev").run_jobs_in_app is True
    assert Settings(APP_ENV="prod").run_jobs_in_app is False
    assert Settings(APP_ENV="prod", RUN_JOBS_IN_APP=True).run_jobs_in_app is True
