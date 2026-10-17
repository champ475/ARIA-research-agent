"""
utils/llm_json.py — reliable structured output from LLM replies (LLM-05).

    result = ask_json(client, prompt, QueryAnalysis, node="query_analyzer", max_tokens=200)

1. `extract_json` finds the JSON in whatever the model returned: inside a
   ```json fence, after a sentence of prose, with trailing commas, or cut off
   mid-object (closed for it). It never raises anything but ParseFailure.
2. `parse_model` validates that against a pydantic model.
3. `ask_json` makes the call; if the reply can't be used it asks once more,
   quoting the validation error and the schema; if that fails too it returns
   None (the caller's safe fallback) and counts a `parse_failure` for the node
   on the turn record (OBS-03 reads the rate).

Nothing here strips "```json" with str.strip — that removes any of those
characters from both ends, e.g. a bare `null` became `ull`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

# Marks the one repair attempt (the eval replay treats an unrecorded repair
# as a failed one instead of a missing fixture).
REPAIR_MARKER = "\n\n[format-repair]\n"

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


class IntakeReply(BaseModel):
    """Planning intake (VAL-05 validates `updates` field by field)."""

    updates: dict[str, Any] = {}
    reply: str | None = None


class ParseFailure(ValueError):
    """The reply holds no usable JSON (or it doesn't fit the schema)."""


def _balanced_span(text: str, start: int) -> tuple[str, bool]:
    """The JSON value starting at text[start] ('{' or '['), string-aware.
    Returns (span, complete); an unfinished value is closed for the caller."""
    stack: list[str] = []
    in_str = escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return text[start : i + 1], False
            stack.pop()
            if not stack:
                return text[start : i + 1], True
    # Truncated: close any open string and brackets.
    tail = text[start:]
    if in_str:
        tail += '"'
    tail = re.sub(r",\s*$", "", tail.rstrip())
    tail = re.sub(r":\s*$", ": null", tail)
    return tail + "".join(reversed(stack)), False


def extract_json(raw: Any) -> Any:
    """The first JSON object/array in `raw`. Raises ParseFailure."""
    if not isinstance(raw, str):
        raise ParseFailure("reply is not text")
    text = raw.strip()
    if not text:
        raise ParseFailure("empty reply")
    fenced = _FENCE.search(text)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(text)
    for cand in candidates:
        for opener in ("{", "["):
            pos = cand.find(opener)
            if pos < 0:
                continue
            span, _complete = _balanced_span(cand, pos)
            for attempt in (span, _TRAILING_COMMA.sub(r"\1", span)):
                try:
                    return json.loads(attempt)
                except json.JSONDecodeError:
                    continue
    try:
        return json.loads(text)  # a bare scalar ("null", "3")
    except json.JSONDecodeError:
        raise ParseFailure("no JSON object in the reply") from None


def parse_model(raw: Any, model: type[M]) -> M:
    """The reply validated as `model`. Raises ParseFailure with a short reason."""
    data = extract_json(raw)
    try:
        return model.model_validate(data)
    except ValidationError as e:
        errors = e.errors()
        if not errors:
            raise ParseFailure("invalid") from None
        loc = ".".join(str(p) for p in errors[0]["loc"]) or "(root)"
        raise ParseFailure(f"{loc}: {errors[0]['msg']}") from None


def _schema_hint(model: type[BaseModel]) -> str:
    props = model.model_json_schema().get("properties", {})
    return json.dumps({k: v.get("type", "any") for k, v in props.items()})


def record_parse_failure(node: str, reason: str) -> None:
    from utils import tracing

    log.warning("[llm_json] parse_failure node=%s reason=%s", node, reason)
    rec = tracing.current()
    if rec is not None:
        rec.add_parse_failure(node)


def ask_json(client: Any, prompt: str, schema: type[M], /, *, node: str, **ask_kwargs: Any) -> M | None:
    """Ask, validate, repair once, else None (and count the failure).
    `ask_kwargs` go to client.ask (including its own `model` override)."""
    raw = client.ask(prompt, **ask_kwargs)
    try:
        return parse_model(raw, schema)
    except ParseFailure as first:
        reason = str(first)
    repair = (
        prompt
        + REPAIR_MARKER
        + f"Your previous reply could not be used ({reason}). Reply again with ONLY a JSON object, "
        + f"no prose and no code fence, with these fields: {_schema_hint(schema)}"
    )
    try:
        return parse_model(client.ask(repair, **ask_kwargs), schema)
    except ParseFailure as second:
        record_parse_failure(node, str(second))
        return None


def json_reply(client: Any, schema: type[BaseModel], node: str, prompt: str, /, **ask_kwargs: Any) -> str:
    """ask_json for nodes that post-process the JSON themselves: the validated
    object re-serialised, or "" when it couldn't be obtained (the node's own
    fallback then applies)."""
    result = ask_json(client, prompt, schema, node=node, **ask_kwargs)
    return result.model_dump_json() if result is not None else ""
