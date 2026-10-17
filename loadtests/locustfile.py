"""
loadtests/locustfile.py — the repeatable load profile (TEST-06).

Each virtual user is a signed-in person chatting through the real Gradio queue
API (loadtests/gradio_http.py): 60% equity questions, 15% mutual funds, 10%
screener, 10% alerts, 5% reports. It records, per kind of question, the time to
the first streamed answer text (TTFT) and the time to the end of the turn, and
counts any turn that ends in an error message as a failure.

Run it against a server using the fake LLM and fake data with realistic
latency (loadtests/run.py does the whole thing), never against production:

    locust -f loadtests/locustfile.py --headless -u 20 -r 5 -t 5m \
        --host http://localhost:7870 --csv test-results/load/run

Environment: LOAD_PASSWORD (default below), LOAD_THINK_MIN / LOAD_THINK_MAX
seconds between questions (default 2 and 6).
"""

from __future__ import annotations

import itertools
import os
import random

from locust import User, between, events, task

from loadtests.gradio_http import ChatSession, login

PASSWORD = os.environ.get("LOAD_PASSWORD", "LoadTest-Passw0rd-9!")

QUESTIONS = {
    "equity": [
        "Analyse TCS",
        "How is INFY doing?",
        "RELIANCE technicals",
        "HDFCBANK fundamentals",
        "Latest news on ITC",
    ],
    "mf": ["Best ELSS for tax saving", "Top flexi cap funds", "Compare large cap funds"],
    "screener": ["IT stocks with PE below 40", "Banking stocks with ROE above 12"],
    "alerts": ["show my alerts", "Alert me if INFY goes above 99999"],
    "report": ["Generate an IC report on TCS"],
}

_ids = itertools.count(1)


class Chatter(User):
    wait_time = between(
        float(os.environ.get("LOAD_THINK_MIN", 2)), float(os.environ.get("LOAD_THINK_MAX", 6))
    )

    def on_start(self) -> None:
        self.email = f"load+{os.getpid()}-{next(_ids)}@example.com"
        self.chat = ChatSession(self.host, login(self.host, self.email, PASSWORD))

    def _ask(self, kind: str) -> None:
        turn = self.chat.ask(random.choice(QUESTIONS[kind]))  # noqa: S311  # nosec B311 - load profile, not security
        exc = RuntimeError(turn.error) if turn.error else None
        self._note_degraded(kind, turn)
        events.request.fire(
            request_type="TTFT",
            name=kind,
            response_time=(turn.ttft_s if turn.ttft_s is not None else turn.total_s) * 1000,
            response_length=0,
            exception=exc,
            context={},
        )
        events.request.fire(
            request_type="TURN",
            name=kind,
            response_time=turn.total_s * 1000,
            response_length=0,
            exception=exc,
            context={},
        )

    def _note_degraded(self, kind: str, turn) -> None:
        """An answer that names missing data is a success, but worth counting under faults."""
        if turn.degraded and not turn.error:
            events.request.fire(
                request_type="DEGRADED",
                name=kind,
                response_time=turn.total_s * 1000,
                response_length=0,
                exception=None,
                context={},
            )

    @task(60)
    def equity(self) -> None:
        self._ask("equity")

    @task(15)
    def mutual_funds(self) -> None:
        self._ask("mf")

    @task(10)
    def screener(self) -> None:
        self._ask("screener")

    @task(10)
    def alerts(self) -> None:
        self._ask("alerts")

    @task(5)
    def report(self) -> None:
        self._ask("report")
