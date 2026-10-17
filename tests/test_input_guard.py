"""VAL-01 — one input contract for every chat message."""

import unicodedata
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agents.llm_client import LLMProvidersExhausted, format_exhausted_message
from config import MAX_CHAT_CHARS
from ui import chat as chat_ui
from utils.input_guard import CleanMessage, InputRejected, clean_message

_INVISIBLE = {"​", "‌", "‍", "⁠", "﻿", "­"}


def _assert_clean(text: str):
    assert text == text.strip()
    assert 0 < len(text) <= MAX_CHAT_CHARS
    for ch in text:
        assert ch == "\n" or unicodedata.category(ch) not in {"Cc", "Cf", "Co", "Cs", "Cn"}, repr(ch)
        assert ch not in _INVISIBLE


@settings(max_examples=500, deadline=None)
@given(st.text(max_size=6000))
def test_any_unicode_text_gives_a_typed_result_and_never_raises(raw):
    result = clean_message(raw)
    assert isinstance(result, (CleanMessage, InputRejected))
    if isinstance(result, CleanMessage):
        _assert_clean(result.text)


@settings(max_examples=200, deadline=None)
@given(st.one_of(st.none(), st.integers(), st.binary(), st.lists(st.text()), st.floats()))
def test_non_text_input_is_rejected_not_raised(raw):
    result = clean_message(raw)
    assert isinstance(result, InputRejected) and result.reason == "not_text"


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet=st.characters(categories=["Cc", "Cf", "Zs", "Zl", "Zp"]), max_size=200))
def test_invisible_only_input_is_empty(raw):
    result = clean_message(raw)
    assert isinstance(result, InputRejected) and result.reason == "empty"


@pytest.mark.parametrize(
    ("length", "accepted"), [(MAX_CHAT_CHARS - 1, True), (MAX_CHAT_CHARS, True), (MAX_CHAT_CHARS + 1, False)]
)
def test_length_boundaries(length, accepted):
    result = clean_message("a" * length)
    assert isinstance(result, CleanMessage) is accepted
    if not accepted:
        assert result.reason == "too_long"
        assert f"{MAX_CHAT_CHARS:,}" in result.user_message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("T​CS", "TCS"),
        ("﻿RELIANCE price", "RELIANCE price"),
        ("ＴＣＳ　ＰＥ", "TCS PE"),  # full-width letters + ideographic space (NFKC)
        ("RSI below ３０", "RSI below 30"),  # full-width digits
        ("  alert   me\t\tif  TCS  < 3500  ", "alert me if TCS < 3500"),
        ("line1\r\nline2", "line1\nline2"),
        ("a\n\n\n\n\nb", "a\n\nb"),
        ("TCS\x00\x07\x1b[31m", "TCS[31m"),
        ("📊 TCS analysis", "📊 TCS analysis"),  # emoji are not stripped
        ("टीसीएस का भाव", "टीसीएस का भाव"),  # Devanagari kept intact
    ],
)
def test_normalisation(raw, expected):
    result = clean_message(raw)
    assert isinstance(result, CleanMessage)
    assert result.text == expected


@pytest.mark.parametrize("raw", ["", "   ", "\n\t\n", "​​", "\x00"])
def test_empty_after_cleaning(raw):
    assert clean_message(raw) == InputRejected("empty", "")


def test_nfkc_expansion_counts_toward_the_limit():
    # U+FDFA expands to 18 characters under NFKC; the limit applies after normalisation.
    result = clean_message("ﷺ" * 300)
    assert isinstance(result, InputRejected) and result.reason == "too_long"


def test_too_large_error_is_not_labelled_rate_limited():
    msg = format_exhausted_message(
        LLMProvidersExhausted(
            RuntimeError("Error code: 413 - Request too large"), is_byo=False, too_large=True
        )
    )
    assert "too long" in msg
    assert "rate-limited" not in msg


def test_stream_exhaustion_from_oversized_prompt_sets_too_large():
    from agents.llm_client import LLMClient

    client = LLMClient.__new__(LLMClient)
    provider = MagicMock()
    provider.name = "p1"
    provider.stream.side_effect = RuntimeError("413 request too large")
    client._primary, client._secondary, client._is_byo = provider, None, False
    with pytest.raises(LLMProvidersExhausted) as exc:
        list(client.stream("x" * 10))
    assert exc.value.too_large is True


def _drain(gen):
    return list(gen)


def test_respond_rejects_too_long_without_calling_the_orchestrator(app_module):
    with (
        patch("services.chat.get_orchestrator") as get_orch,
        patch("services.chat.persist_turn") as persist,
    ):
        outputs = _drain(chat_ui.respond(1, "x" * (MAX_CHAT_CHARS + 50), [], [], 7, "1M"))
    get_orch.assert_not_called()
    persist.assert_not_called()
    (final,) = outputs
    history = final[2]
    assert "limit is" in history[-1]["content"]
    assert len(history[-2]["content"]) < 300  # the giant message isn't echoed back in full


def test_respond_uses_cleaned_text_downstream(app_module):
    orch = MagicMock()
    orch.classify.return_value = "chat"
    orch.process_stream.return_value = iter([("ok", None, None, None, None, "chat")])
    orch.generate_suggestions.return_value = []
    with (
        patch("services.chat.get_orchestrator", return_value=orch),
        patch("services.chat.persist_turn"),
    ):
        _drain(chat_ui.respond(1, "T​CS\x00 price", [], [], 7, "1M"))
    orch.process_stream.assert_called_once_with("TCS price", progress=True)
