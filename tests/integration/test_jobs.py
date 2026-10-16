"""REL-05 on a real Postgres: one owner per job across processes, a
heartbeat per run, and a single Telegram poller."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from utils import jobs
from utils.jobs import Job

pytestmark = pytest.mark.integration


def test_only_one_holder_per_job(db):
    with jobs.job_lock("alert_check") as first:
        assert first is not None
        with jobs.job_lock("alert_check") as second, jobs.job_lock("housekeeping") as other:
            assert second is None and other is not None
    with jobs.job_lock("alert_check") as again:  # released on exit
        assert again is not None


def test_run_records_a_heartbeat_and_is_not_repeated(db):
    calls = []
    job = Job("housekeeping", lambda: calls.append(1), every=timedelta(hours=1))
    assert jobs.run_job(job) == "ran"
    assert jobs.run_job(job) == "not_due"  # another run right after: nothing to do
    beat = db.job_heartbeats()[0]
    assert beat["job"] == "housekeeping" and beat["last_status"] == "ok" and beat["runs"] == 1
    assert beat["owner"] == jobs.OWNER and beat["last_ok_at"] is not None and calls == [1]


def test_a_failure_is_recorded_without_its_message(db):
    def boom():
        raise RuntimeError("password=hunter2")

    assert jobs.run_job(Job("mf_refresh", boom, every=timedelta(hours=1))) == "failed"
    beat = db.job_heartbeats()[0]
    assert (beat["last_status"], beat["last_error"], beat["failures"]) == ("error", "RuntimeError", 1)
    assert beat["last_ok_at"] is None


def test_two_workers_run_a_job_exactly_once(db):
    runs = []

    def slow():
        runs.append(threading.current_thread().name)
        time.sleep(1.0)

    job = Job("alert_check", slow, every=timedelta(minutes=5))
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        outcomes.append(jobs.run_job(job))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) in (["ran", "skipped"], ["not_due", "ran"]) and len(runs) == 1


def test_one_telegram_poller_and_a_standby_takes_over(db):
    steps = {"a": 0, "b": 0}
    stop_a, stop_b = threading.Event(), threading.Event()

    def step(name):
        def _s():
            steps[name] += 1
            time.sleep(0.05)

        return _s

    a = threading.Thread(target=jobs.run_service, args=("telegram_poller", step("a"), stop_a, 60.0, 0.2))
    a.start()
    time.sleep(0.5)
    b = threading.Thread(target=jobs.run_service, args=("telegram_poller", step("b"), stop_b, 60.0, 0.2))
    b.start()
    time.sleep(0.8)
    assert steps["a"] > 0 and steps["b"] == 0  # b is on standby
    stop_a.set()
    a.join(10)
    assert not a.is_alive()
    time.sleep(1.0)
    assert steps["b"] > 0  # the lock was freed and b took over
    stop_b.set()
    b.join(10)
    assert not b.is_alive()  # nothing may outlive the test and hold a session
    assert db.job_heartbeats()[0]["job"] == "telegram_poller"


def test_expired_link_codes_are_purged(db, two_users):
    a, _ = two_users
    db.telegram_link_code_create(a, "old", datetime.now(UTC) - timedelta(minutes=1))
    db.telegram_link_code_create(a, "new", datetime.now(UTC) + timedelta(minutes=10))
    assert db.telegram_link_codes_purge_expired() == 1
    assert db.telegram_link_code_resolve("new") == a
