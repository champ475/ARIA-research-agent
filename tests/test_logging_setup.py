"""OBS-01 — structured JSON logging, turn context, redaction, no print()."""

import ast
import contextvars
import io
import json
import logging
import pathlib
import threading

import pytest

from utils import logging_setup as ls

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def captured(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    buf = io.StringIO()
    ls.configure_logging(level="DEBUG", stream=buf)
    yield buf
    ls._configured = False


def _lines(buf):
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_every_record_is_json_with_both_timestamps(captured):
    logging.getLogger("t").info("hello %s", "world")
    (rec,) = _lines(captured)
    assert rec["msg"] == "hello world"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "t"
    assert rec["ts_utc"].endswith("+00:00")
    assert rec["ts_ist"].endswith("+05:30")


def test_turn_fields_present_inside_context_and_blank_outside(captured):
    log = logging.getLogger("t")
    with ls.turn_context(user_id=42, session_id=7) as tid:
        log.info("inside")
    log.info("outside")
    inside, outside = _lines(captured)
    assert inside["turn_id"] == tid and len(tid) == 32
    assert inside["session_id"] == "7"
    assert inside["user"] and inside["user"] != "42"  # pseudonymised, never the raw id
    assert outside["turn_id"] == "" and outside["user"] == "" and outside["session_id"] == ""


def test_pseudonym_is_stable_and_keyed(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a")
    first = ls.pseudonymise_user(5)
    assert first == ls.pseudonymise_user(5)
    assert first != ls.pseudonymise_user(6)
    monkeypatch.setenv("SECRET_KEY", "b")
    assert ls.pseudonymise_user(5) != first
    monkeypatch.delenv("SECRET_KEY")
    assert ls.pseudonymise_user(5) == "anon"


@pytest.mark.parametrize(
    "secret",
    [
        "gsk_abcdefghijklmnopqrstuvwxyz0123",
        "sk-proj-abcdefghijklmnop1234567890",
        "AIzaSyA1234567890abcdefghijklmnopqrs",
        "gAAAAABmZ2abcdefghijklmnopqrstuvwxyz0123456789",
        "Bearer abcdefghijklmnop.qrstu",
        "api_key=supersecretvalue123",
        "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
    ],
)
def test_secrets_are_redacted(captured, secret):
    logging.getLogger("t").warning(f"upstream said: {secret} end")
    (rec,) = _lines(captured)
    assert secret not in rec["msg"]
    assert "***" in rec["msg"]


def test_exception_text_is_redacted(captured):
    try:
        raise RuntimeError("bad key gsk_abcdefghijklmnopqrstuvwxyz0123")
    except RuntimeError:
        logging.getLogger("t").exception("boom")
    (rec,) = _lines(captured)
    assert "gsk_abcdefghijklmnopqrstuvwxyz0123" not in rec["exc"]
    assert "RuntimeError" in rec["exc"]


def test_run_in_turn_keeps_turn_id_across_yields_in_fresh_contexts(captured):
    """Gradio resumes each generator step in a newly copied context on a
    worker thread; every step must still log the same turn_id."""
    log = logging.getLogger("t")

    def handler():
        log.info("step1")
        yield 1
        log.info("step2")
        yield 2

    gen = ls.run_in_turn(handler(), user_id=1, session_id=2)
    # Drive each step from a different thread with an empty context, as Gradio does.
    for _ in range(2):
        t = threading.Thread(target=lambda: contextvars.Context().run(next, gen))
        t.start()
        t.join()
    a, b = _lines(captured)
    assert a["turn_id"] and a["turn_id"] == b["turn_id"]
    assert a["session_id"] == b["session_id"] == "2"


def test_run_in_turn_closes_inner_generator_on_early_stop():
    closed = []

    def handler():
        try:
            yield 1
            yield 2
        finally:
            closed.append(True)

    gen = ls.run_in_turn(handler(), user_id=1)
    next(gen)
    gen.close()
    assert closed == [True]


def _prints_outside_main(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    main_lines: set[int] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and getattr(node.test.left, "id", None) == "__name__"
        ):
            main_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "print" and n.lineno not in main_lines
    ]


def test_no_print_in_application_packages():
    offenders = {}
    files = [REPO / "app.py"]
    for pkg in ("agents", "graph", "tools", "utils"):
        files += list((REPO / pkg).rglob("*.py"))
    for f in files:
        hits = _prints_outside_main(f)
        if hits:
            offenders[str(f.relative_to(REPO))] = hits
    assert offenders == {}, f"print() found (use logging): {offenders}"
