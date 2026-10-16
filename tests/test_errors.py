"""SEC-08: users get a friendly message and an error ID, never exception
class names, SQL, file paths or provider responses."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from utils.errors import (
    ConfigurationError,
    DataUnavailableError,
    InternalError,
    UserInputError,
    error_id_from_turn,
    render_error,
)

ROOT = Path(__file__).resolve().parents[1]
LEAKS = re.compile(r"OperationalError|Traceback|SELECT |INSERT |File \"|\.py\b|RuntimeError|<html", re.I)


def test_error_id_is_the_turn_id_prefix():
    assert error_id_from_turn("3f9a1c2b-0000-4000-8000-000000000000") == "3f9a1c2b"
    assert error_id_from_turn("") == "unknown"


def test_render_unknown_exception_is_generic_with_error_id():
    text = render_error(None, "abcd1234")
    assert "Something went wrong" in text and "abcd1234" in text


def test_render_typed_errors_use_only_their_user_message():
    e = ConfigurationError(
        "Your saved API key can't be read any more.",
        fix_hint="Enter it again in Settings.",
        detail="InvalidToken at /app/utils/crypto.py SECRET_KEY",
    )
    text = render_error(e, "abcd1234")
    assert "can't be read" in text and "Settings" in text
    assert "crypto.py" not in text and "SECRET_KEY" not in text
    assert "abcd1234" not in text  # a settings problem, not our failure
    assert "abcd1234" in render_error(DataUnavailableError(), "abcd1234")
    assert "abcd1234" in render_error(InternalError(), "abcd1234")
    assert render_error(UserInputError("Too long."), "x").startswith("⚠️ Too long.")


def _orch_raising(exc: BaseException):
    orch = MagicMock()
    orch.classify.return_value = "chat"

    def stream(_msg, progress=False):
        raise exc
        yield  # pragma: no cover - makes this a generator

    orch.process_stream.side_effect = stream
    return orch


def _final_reply(app_module, exc: BaseException) -> str:
    with (
        patch.object(app_module, "get_orchestrator", return_value=_orch_raising(exc)),
        patch.object(app_module, "_persist_turn"),
    ):
        frames = list(app_module.respond(7, "what is TCS trading at", [], [], 1, "1M"))
    return frames[-1][2][-1]["content"]


@pytest.mark.parametrize(
    "exc",
    [
        psycopg.OperationalError(
            'connection to server at "10.0.0.5" failed: SELECT * FROM users WHERE id=7 '
            '(File "/app/utils/db.py", line 101)'
        ),
        RuntimeError("Groq 500: <html><body>upstream error at /v1/chat/completions</body></html>"),
        KeyError("currentPrice"),
        ValueError("could not convert string to float: 'N/A' in tools/analysis/fundamental.py"),
    ],
)
def test_internal_failures_never_leak_detail(app_module, exc):
    reply = _final_reply(app_module, exc)
    assert "Error ID" in reply
    assert not LEAKS.search(reply), reply
    assert "10.0.0.5" not in reply and "currentPrice" not in reply


def test_configuration_error_shows_its_fix(app_module):
    exc = ConfigurationError(
        "No API key is configured for your LLM provider.",
        fix_hint="Add one in Settings.",
        detail="secret detail",
    )
    reply = _final_reply(app_module, exc)
    assert "No API key is configured" in reply and "Add one in Settings" in reply
    assert "secret detail" not in reply


# ── static guard: exception text never formatted into user-facing strings ────

SCAN = ["app.py", "agents", "graph", "utils", "tools"]
# Internal (never shown) uses of exception text, reviewed:
ALLOWED = {
    ("agents/llm_client.py", "_classify_error"),  # str(e).lower() to classify, not displayed
    ("utils/tracing.py", "span"),  # redacted error field on the trace span
    ("utils/llm_json.py", "ask_json"),  # parse-failure reason: logged and fed back to the model, never shown
    ("agents/graph_orchestrator.py", "generate_suggestions"),  # parse-failure reason, logged only
}
_LOG_ATTRS = {"debug", "info", "warning", "error", "exception", "critical"}


def _is_log_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_ATTRS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in ("log", "logger", "logging")
    )


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    rel = path.relative_to(ROOT).as_posix()
    found = []
    for handler in ast.walk(tree):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name):
            continue
        name = handler.name
        for node in ast.walk(handler):
            uses = (
                isinstance(node, ast.FormattedValue)
                and isinstance(node.value, ast.Name)
                and node.value.id == name
            ) or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("str", "repr")
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == name
            )
            if not uses:
                continue
            anc, func_name, safe = node, None, False
            while anc in parents:
                anc = parents[anc]
                if _is_log_call(anc) or isinstance(anc, ast.Raise):
                    safe = True
                    break
                if isinstance(anc, ast.FunctionDef) and func_name is None:
                    func_name = anc.name
            if not safe and (rel, func_name) not in ALLOWED:
                found.append(f"{rel}:{node.lineno} in {func_name}")
    return found


def test_no_exception_text_in_user_facing_strings():
    files = [ROOT / "app.py"] + [p for d in SCAN[1:] for p in (ROOT / d).rglob("*.py")]
    violations = [v for f in files for v in _violations(f)]
    assert violations == [], "exception text reaches a non-log string:\n" + "\n".join(violations)
