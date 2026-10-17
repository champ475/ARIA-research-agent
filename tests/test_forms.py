"""VAL-07: Watchlist and Settings inputs are validated before anything is saved."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from utils.errors import ConfigurationError, UserInputError
from utils.forms import llm_settings, probe_llm, watchlist_symbol


@pytest.mark.parametrize(
    ("raw", "symbol"),
    [
        ("TCS", "TCS"),
        ("tcs", "TCS"),
        ("  infy ", "INFY"),
        ("Reliance Industries", "RELIANCE"),
        ("M&M", "M&M"),
    ],
)
def test_listed_symbols_and_names_resolve(raw, symbol):
    assert watchlist_symbol(raw) == symbol


@pytest.mark.parametrize(
    "raw", ["<script>alert(1)</script>", '"><img onerror=1>', "XYZQ", "", "   ", "a" * 61]
)
def test_invalid_entries_are_refused(raw):
    with pytest.raises(UserInputError):
        watchlist_symbol(raw)


def test_unknown_name_gets_suggestions():
    with pytest.raises(UserInputError) as e:
        watchlist_symbol("Relaince")
    assert "RELIANCE" in e.value.user_message


@pytest.mark.parametrize(
    ("model", "key", "url", "fragment"),
    [
        ("", "sk-abc", None, "model name"),
        ("gpt 4o", "sk-abc", None, "one word"),
        ("m" * 101, "sk-abc", None, "one word"),
        ("gpt-4o-mini", "", None, "API key"),
        ("gpt-4o-mini", "sk abc", None, "doesn't look like"),
        ("gpt-4o-mini", "k" * 301, None, "doesn't look like"),
        ("gpt-4o-mini", "sk-abc", "http://openai.com", "https"),
        ("gpt-4o-mini", "sk-abc", "https://127.0.0.1/v1", ""),
    ],
)
def test_llm_settings_format_checks(model, key, url, fragment):
    with pytest.raises(UserInputError) as e:
        llm_settings(model, key, url)
    assert fragment.lower() in e.value.user_message.lower()


def test_valid_llm_settings_are_cleaned():
    assert llm_settings(" gpt-4o-mini ", " sk-abc ", "") == ("gpt-4o-mini", "sk-abc", None)


def test_bad_key_surfaces_a_clear_settings_error_without_raw_provider_text():
    class AuthenticationError(Exception):
        status_code = 401

    def fail(self, *a, **k):
        raise AuthenticationError("Error code: 401 - {'error': 'Incorrect API key provided: sk-abc...'}")

    with patch("agents.providers.openai_compatible.OpenAICompatibleProvider.ask", fail):
        with pytest.raises(ConfigurationError) as e:
            probe_llm("gpt-4o-mini", "sk-abc", "https://api.openai.com/v1")
    assert "didn't accept" in e.value.user_message
    assert "Incorrect API key provided" not in e.value.user_message


def test_probe_uses_a_short_timeout():
    seen = {}

    def ok(self, *a, **k):
        seen["timeout"] = self._request_timeout
        return "OK"

    with patch("agents.providers.openai_compatible.OpenAICompatibleProvider.ask", ok):
        probe_llm("gpt-4o-mini", "sk-abc", None)
    assert seen["timeout"] == 10
