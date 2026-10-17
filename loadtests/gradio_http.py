"""
loadtests/gradio_http.py — drive ARIA's chat over Gradio's plain HTTP queue (TEST-06).

Locust runs its users as gevent greenlets, and `gradio_client` (threads and a
background SSE reader) never returns a result under gevent. The queue protocol
itself is small, so this speaks it directly with `requests`, which gevent
handles well:

    POST /gradio_api/queue/join                    start an event (inputs, fn_index, session_hash)
    GET  /gradio_api/queue/data?session_hash=...   server-sent events: process_starts,
                                                   process_generating (streamed updates),
                                                   process_completed

One `ChatSession` is one virtual browser tab: it keeps its session_hash, so the
server-side State (the open chat) carries from one question to the next, as it
does for a person. It does what the page does: authenticates with the session
cookie, and sends the chat event's inputs with `None` for State components.
"""

from __future__ import annotations

import json
import random
import re
import string
import time
from dataclasses import dataclass

import requests

# Texts of the answers that mean the turn failed (SEC-08 error boundary, provider outage, bad input)...
ERROR_MARKS = (
    "Something went wrong",
    "isn't available right now",
    "too long for the model",
    "was rejected by your provider",
)
# ...and the line a degraded-but-useful answer carries when some data did not arrive (REL-06).
DEGRADED_MARK = "Data not available this time"
_MARKERS = ("aria-dot", "aria-progress")


def login(host: str, email: str, password: str) -> str:
    """Sign up (or log in) through the HTML forms; returns the aria_session cookie."""
    s = requests.Session()
    for page, extra in (("/signup", {"agree": "yes"}), ("/login", {})):
        html = s.get(host + page, timeout=30).text
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        if m:
            s.post(
                host + page,
                data={"email": email, "password": password, "csrf": m.group(1), **extra},
                timeout=30,
            )
        if s.cookies.get("aria_session"):
            return s.cookies["aria_session"]
    raise RuntimeError(f"could not log in as {email}")


@dataclass
class Turn:
    ttft_s: float | None  # time to the first streamed answer text
    total_s: float
    error: str | None  # None when the turn produced a normal answer
    degraded: bool = False  # answered, but named data that did not arrive


def _is_answer_frame(raw: str) -> bool:
    """A streamed update that carries answer text: not the loading dots, not the
    research-progress line. (Updates after the first are diffs, so the markers
    only appear in the frames that set them.)"""
    return (not any(m in raw for m in _MARKERS) and '"content"' in raw) or "Here is what" in raw


class ChatSession:
    def __init__(self, host: str, cookie: str, api_name: str = "_respond", timeout: float = 180.0):
        self.host, self.timeout = host.rstrip("/"), timeout
        self.http = requests.Session()
        self.http.cookies.set("aria_session", cookie)
        self.session_hash = "".join(random.choices(string.ascii_lowercase + string.digits, k=11))  # noqa: S311  # nosec B311 - load profile, not security
        cfg = self.http.get(self.host + "/config", timeout=30).json()
        components = {c["id"]: c for c in cfg["components"]}
        self.dep = next(d for d in cfg["dependencies"] if d.get("api_name") == api_name)
        self.types = [components[i]["type"] for i in self.dep["inputs"]]
        targets = self.dep.get("targets") or []
        self.trigger_id = targets[0][0] if targets else None

    def _inputs(self, text: str) -> list:
        values = {"textbox": text, "chatbot": [], "radio": "1D"}
        return [None if t == "state" else values.get(t) for t in self.types]

    def ask(self, text: str) -> Turn:
        start = time.perf_counter()
        first: float | None = None
        error: str | None = None
        degraded = False
        try:
            r = self.http.post(
                self.host + "/gradio_api/queue/join",
                json={
                    "data": self._inputs(text),
                    "fn_index": self.dep["id"],
                    "session_hash": self.session_hash,
                    "trigger_id": self.trigger_id,
                },
                timeout=30,
            )
            r.raise_for_status()
            with self.http.get(
                self.host + "/gradio_api/queue/data",
                params={"session_hash": self.session_hash},
                stream=True,
                timeout=self.timeout,
            ) as stream:
                stream.raise_for_status()
                for line in stream.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    msg = json.loads(line[5:])
                    kind = msg.get("msg")
                    if kind in ("process_generating", "process_completed"):
                        if any(m in line for m in ERROR_MARKS):
                            error = "the turn ended in an error message"
                        if DEGRADED_MARK in line:
                            degraded = True
                        if first is None and _is_answer_frame(line):
                            first = time.perf_counter() - start
                    if kind == "process_completed":
                        if not msg.get("success", True):
                            error = error or "the event failed"
                        break
                    if kind == "close_stream":
                        error = error or "stream closed early"
                        break
        except Exception as e:  # noqa: BLE001 - a load test reports every failure
            error = f"{type(e).__name__}: {e}"[:200]
        total = time.perf_counter() - start
        if error is None and first is None:
            error = "no answer text streamed"
        return Turn(first, total, error, degraded)
