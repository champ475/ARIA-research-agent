"""
Covers the model-override addition to LLMClient (agents/llm_client.py), which
the graph work needs to route QueryAnalyzer/Reflection/Verification's cheap
calls through GROQ_MODEL_FAST instead of the primary model (RFC Sec17).
"""

from unittest.mock import MagicMock, patch

import pytest

from agents.llm_client import (
    LLMClient,
    LLMProvidersExhausted,
    _classify_error,
    _GroqProvider,
    _ProviderHealthCache,
    format_exhausted_message,
)
from config import GROQ_MODEL, GROQ_MODEL_FAST


def _make_groq_completion(text="ok"):
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=text))]
    return completion


@pytest.fixture
def groq_provider(monkeypatch):
    monkeypatch.setattr("agents.llm_client._groq_rotator.available", lambda: True)
    monkeypatch.setattr("agents.llm_client._groq_rotator.next", lambda: "fake-key")
    provider = _GroqProvider(system="test system")
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_groq_completion()
    provider._client = lambda: fake_client
    return provider, fake_client


class TestGroqProviderModelOverride:
    def test_ask_defaults_to_primary_model(self, groq_provider):
        provider, fake_client = groq_provider
        provider.ask("hello")
        _, kwargs = fake_client.chat.completions.create.call_args
        assert kwargs["model"] == GROQ_MODEL

    def test_ask_honors_model_override(self, groq_provider):
        provider, fake_client = groq_provider
        provider.ask("hello", model=GROQ_MODEL_FAST)
        _, kwargs = fake_client.chat.completions.create.call_args
        assert kwargs["model"] == GROQ_MODEL_FAST

    def test_chat_honors_model_override(self, groq_provider):
        provider, fake_client = groq_provider
        provider.chat("hello", model=GROQ_MODEL_FAST)
        _, kwargs = fake_client.chat.completions.create.call_args
        assert kwargs["model"] == GROQ_MODEL_FAST


class TestLLMClientThreadsModelThrough:
    def test_ask_passes_model_to_primary_provider(self):
        client = LLMClient.__new__(LLMClient)  # bypass provider init
        fake_primary = MagicMock()
        fake_primary.ask.return_value = "reply"
        client._primary = fake_primary
        client._secondary = None
        client._active = "Groq"

        client.ask("hello", model=GROQ_MODEL_FAST)

        fake_primary.ask.assert_called_once_with("hello", pytest.approx(0.3), 2000, GROQ_MODEL_FAST)


@pytest.fixture(autouse=True)
def _reset_provider_health():
    """agents.llm_client._provider_health is a module-level singleton
    (mirrors _groq_rotator) — reset it around every test in this file so a
    provider marked unavailable by one test can never leak into the next."""
    import agents.llm_client as llm_client_module
    from config import PROVIDER_ZERO_QUOTA_TTL_SECONDS

    original = llm_client_module._provider_health
    llm_client_module._provider_health = _ProviderHealthCache(PROVIDER_ZERO_QUOTA_TTL_SECONDS)
    yield
    llm_client_module._provider_health = original


def _stub_client(primary_side_effect=None, secondary_side_effect=None, is_byo=False):
    client = LLMClient.__new__(LLMClient)
    primary = MagicMock()
    primary.name = "Groq"
    if primary_side_effect is not None:
        primary.ask.side_effect = primary_side_effect
    client._primary = primary
    if secondary_side_effect is not None:
        secondary = MagicMock()
        secondary.name = "Gemini"
        secondary.ask.side_effect = secondary_side_effect
        client._secondary = secondary
    else:
        client._secondary = None
    client._active = "Groq"
    client._is_byo = is_byo
    return client


class TestClassifyError:
    def test_413_classified_too_large(self):
        assert _classify_error(Exception("Error code: 413 - Request too large for model")) == "too_large"

    def test_429_classified_rate_limited(self):
        assert _classify_error(Exception("429 Too Many Requests")) == "rate_limited"

    def test_quota_wording_classified_rate_limited(self):
        assert _classify_error(Exception("You exceeded your current quota")) == "rate_limited"

    def test_zero_quota_marker_classified_zero_quota(self):
        assert _classify_error(Exception("quotaMetric limit: 0, model gemini-2.0-flash")) == "zero_quota"

    def test_unrelated_error_classified_other(self):
        assert _classify_error(Exception("connection reset by peer")) == "other"

    def test_too_large_checked_before_rate_limited_when_both_markers_present(self):
        # Groq's real 413 wording literally contains "rate_limit_exceeded"
        # as an error code string alongside "Request too large" — too_large
        # must win, since retrying (what rate_limited implies) can't fix it.
        msg = "Error code: 413 - Request too large ... 'code': 'rate_limit_exceeded'"
        assert _classify_error(Exception(msg)) == "too_large"


class TestCallTooLargeNeverRetriesSameProvider:
    def test_both_providers_too_large_calls_each_exactly_once_and_never_sleeps(self):
        err = Exception("Error code: 413 - Request too large for model")
        client = _stub_client(primary_side_effect=err, secondary_side_effect=err)

        with patch("agents.llm_client.time.sleep") as mock_sleep:
            with pytest.raises(LLMProvidersExhausted) as exc_info:
                client.ask("hello")

        assert client._primary.ask.call_count == 1
        assert client._secondary.ask.call_count == 1
        mock_sleep.assert_not_called()
        assert exc_info.value.too_large is True

    def test_single_byo_provider_too_large_stops_immediately(self):
        err = Exception("413 request too large")
        client = _stub_client(primary_side_effect=err, is_byo=True)

        with patch("agents.llm_client.time.sleep") as mock_sleep:
            with pytest.raises(LLMProvidersExhausted) as exc_info:
                client.ask("hello")

        assert client._primary.ask.call_count == 1
        mock_sleep.assert_not_called()
        assert exc_info.value.is_byo is True


class TestCallRateLimitedStillRetries:
    def test_rate_limited_retries_up_to_max_retries_with_backoff(self):
        from config import MAX_RETRIES

        err = Exception("429 rate limit exceeded")
        client = _stub_client(primary_side_effect=err, secondary_side_effect=err)

        with patch("agents.llm_client.time.sleep") as mock_sleep:
            with pytest.raises(LLMProvidersExhausted):
                client.ask("hello")

        assert client._primary.ask.call_count == MAX_RETRIES + 1
        assert client._secondary.ask.call_count == MAX_RETRIES + 1
        assert mock_sleep.call_count > 0

    def test_succeeds_once_a_provider_recovers(self):
        client = _stub_client(primary_side_effect=Exception("429 rate limited"))
        client._secondary = MagicMock()
        client._secondary.name = "Gemini"
        client._secondary.ask.return_value = "final answer"

        with patch("agents.llm_client.time.sleep"):
            result = client.ask("hello")

        assert result == "final answer"


class TestCallTotalExhaustionRaises:
    def test_raises_llm_providers_exhausted_with_last_error(self):
        err = Exception("boom")
        client = _stub_client(primary_side_effect=err, secondary_side_effect=err)

        with patch("agents.llm_client.time.sleep"), pytest.raises(LLMProvidersExhausted) as exc_info:
            client.ask("hello")

        assert exc_info.value.last_err is err
        assert exc_info.value.is_byo is False


class TestZeroQuotaProviderSkipped:
    def test_zero_quota_error_marks_provider_unavailable_for_later_calls(self):
        zero_quota_err = Exception("quotaMetric ... limit: 0, model gemini-2.0-flash")
        rate_limited_err = Exception("429 rate limited")
        client = _stub_client(primary_side_effect=rate_limited_err, secondary_side_effect=zero_quota_err)

        with patch("agents.llm_client.time.sleep"), pytest.raises(LLMProvidersExhausted):
            client.ask("hello")

        # Marked unavailable after its FIRST failure — not retried again
        # even within this same call's remaining attempts.
        assert client._secondary.ask.call_count == 1

        import agents.llm_client as llm_client_module

        assert llm_client_module._provider_health.is_unavailable("Gemini") is True

        # A brand new client instance still skips Gemini entirely — the
        # health cache is module-level, not per-instance.
        client2 = _stub_client(primary_side_effect=rate_limited_err)
        client2._secondary = MagicMock()
        client2._secondary.name = "Gemini"
        client2._secondary.ask.side_effect = zero_quota_err

        with patch("agents.llm_client.time.sleep"), pytest.raises(LLMProvidersExhausted):
            client2.ask("hello")

        client2._secondary.ask.assert_not_called()


class TestStreamProviderExhaustion:
    def test_raises_when_nothing_yielded_and_all_providers_fail(self):
        client = _stub_client()
        client._primary.stream.side_effect = Exception("500 server error")

        with pytest.raises(LLMProvidersExhausted):
            list(client.stream("hello"))

    def test_mid_stream_drop_yields_warning_instead_of_raising(self):
        def gen(*args, **kwargs):
            yield "partial "
            raise Exception("connection reset")

        client = _stub_client()
        client._primary.stream.side_effect = gen

        chunks = list(client.stream("hello"))

        assert chunks[0] == "partial "
        assert "dropped mid-response" in chunks[1]


class TestFormatExhaustedMessage:
    def test_trial_path_message_mentions_groq_and_gemini(self):
        exc = LLMProvidersExhausted(Exception("boom"), is_byo=False)
        msg = format_exhausted_message(exc)
        assert "Groq" in msg
        assert "Gemini" in msg

    def test_byo_path_message_points_to_settings_not_trial_instructions(self):
        exc = LLMProvidersExhausted(Exception("boom"), is_byo=True)
        msg = format_exhausted_message(exc)
        assert "Settings" in msg
        assert "API key" in msg
        assert "wait ~30 min" not in msg  # trial-only guidance would mislead a BYO user
