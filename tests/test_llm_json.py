"""LLM-05: structured output — 200+ malformed replies never raise anything
but ParseFailure, usable ones are recovered, and one repair retry is made."""

from __future__ import annotations

import contextlib
import itertools
import json
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from graph.schemas import QueryAnalysis, ToolSelection
from utils.llm_json import ParseFailure, ask_json, extract_json, json_reply, parse_model

OBJECTS = [
    {"intent": "equity", "objective": "analyse TCS", "complexity": "moderate", "companies": ["TCS"]},
    {"tools": ["fetch_info", "compute_fundamentals"]},
    {"missing": [{"missing": "news", "action": "fetch_news", "priority": "high"}]},
    {"issues": []},
    {"updates": {"age": 30}, "reply": "Thanks — what's your horizon?"},
]


def _wrappers(text: str) -> list[str]:
    return [
        text,
        f"```json\n{text}\n```",
        f"```\n{text}\n```",
        f"Sure! Here is the JSON:\n{text}",
        f"{text}\nLet me know if you need anything else.",
        f"Here you go:\n```json\n{text}\n```\nHope this helps.",
        text.replace("}", ",}").replace("]", ",]"),  # trailing commas
        "﻿" + text,  # BOM
        f"  \n\t{text}\n\n",
    ]


RECOVERABLE = [(w, obj) for obj in OBJECTS for w in _wrappers(json.dumps(obj))]


@pytest.mark.parametrize(("raw", "expected"), RECOVERABLE)
def test_recoverable_replies_parse(raw, expected):
    assert extract_json(raw) == expected


def _malformed() -> list[str]:
    out = []
    for obj in OBJECTS:
        text = json.dumps(obj)
        out += [text[:cut] for cut in range(1, len(text), max(1, len(text) // 20))]  # truncated
        out += [w[: len(w) // 2] for w in _wrappers(text)]  # truncated inside wrappers
    out += [
        "",
        "   ",
        "I can't help with that.",
        "{'intent': 'equity'}",  # single quotes
        "```json\n```",
        "null",
        "[]",
        "}{",
        "{{{{",
        "]]]",
        '{"intent": }',
        '{"a": "unterminated',
        '```json\n{"a": 1',
        "NaN",
        "<html>500</html>",
        "\x00\x01\x02",
        "🤖" * 50,
        "{" * 500 + "}" * 499,
    ]
    return out


MALFORMED = _malformed()


def test_fuzz_set_is_large_enough():
    assert len(MALFORMED) + len(RECOVERABLE) >= 200


@pytest.mark.parametrize("raw", MALFORMED)
def test_malformed_never_raises_unexpectedly(raw):
    for model in (QueryAnalysis, ToolSelection):
        with contextlib.suppress(ParseFailure):
            parse_model(raw, model)


def test_truncated_object_is_closed():
    assert extract_json('{"tools": ["fetch_info", "fetch_news"') == {"tools": ["fetch_info", "fetch_news"]}
    assert extract_json('{"intent": "equity", "objective": "anal') == {
        "intent": "equity",
        "objective": "anal",
    }


def test_wrong_shape_is_a_parse_failure():
    with pytest.raises(ParseFailure, match="tools"):
        parse_model('{"tools": "fetch_info"}', ToolSelection)


def test_bare_null_is_not_mangled():
    """str.strip('```json') used to turn 'null' into 'ull'."""
    assert extract_json("null") is None


@settings(max_examples=500, deadline=None)
@given(st.text())
def test_extract_never_raises_other_errors(text):
    with contextlib.suppress(ParseFailure):
        extract_json(text)


def _client(*replies):
    client = MagicMock()
    client.ask.side_effect = list(replies)
    return client


def test_repair_retry_recovers():
    client = _client("Sorry, I think the tools are fetch_info.", '{"tools": ["fetch_info"]}')
    out = ask_json(client, "pick tools", ToolSelection, node="planner", max_tokens=50, model="fast")
    assert out == ToolSelection(tools=["fetch_info"])
    retry_prompt = client.ask.call_args_list[1][0][0]
    assert "could not be used" in retry_prompt and "tools" in retry_prompt
    assert client.ask.call_args_list[1][1] == {"max_tokens": 50, "model": "fast"}


def test_two_failures_return_none_and_count_on_the_turn():
    from utils import tracing

    with tracing.turn("t-1", 1, None, "q") as rec:
        assert json_reply(_client("nope", "still nope"), ToolSelection, "planner", "pick tools") == ""
        assert ask_json(_client('{"tools": 5}', "x"), "q", ToolSelection, node="planner") is None
    assert rec.parse_failures == {"planner": 2}


def test_good_first_reply_makes_one_call():
    client = _client(json.dumps({"tools": ["fetch_news"]}))
    assert ask_json(client, "q", ToolSelection, node="planner").tools == ["fetch_news"]
    assert client.ask.call_count == 1


@pytest.mark.parametrize("bad", list(itertools.islice(MALFORMED, 0, 200, 20)))
def test_nodes_fall_back_without_raising(bad):
    from graph.nodes.planner import _parse_tool_selection
    from graph.nodes.query_analyzer import _parse

    assert _parse(bad)["intent"] in {"chat", "equity"}
    assert isinstance(_parse_tool_selection(bad), list)
