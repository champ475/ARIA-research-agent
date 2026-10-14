"""
Local LLM fallback (agents/providers/local_llm.py + LLMClient's third tier):
used only after every cloud provider failed, at most once per call, also
behind BYO keys, and never subject to the SEC-01 public-host check (its URL
is operator config, not user input).
"""

from unittest.mock import MagicMock

import pytest

import agents.llm_client as llm_client_module
import agents.providers.local_llm as local_llm_module
from agents.llm_client import LLMClient, LLMProvidersExhausted, _classify_error, _ProviderHealthCache
from agents.providers.local_llm import LocalLLMProvider


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(llm_client_module, "_provider_health", _ProviderHealthCache(3600))
    monkeypatch.setattr(llm_client_module.time, "sleep", lambda s: None)


@pytest.fixture
def local_on(monkeypatch):
    monkeypatch.setattr(local_llm_module, "LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080/v1")


def _provider(name, **side_effects):
    p = MagicMock()
    p.name = name
    for method, effect in side_effects.items():
        getattr(p, method).side_effect = effect
    return p


def _client(primary, secondary=None, local=None, is_byo=False):
    c = LLMClient.__new__(LLMClient)
    c._primary, c._secondary, c._local = primary, secondary, local
    c._active = primary.name if primary else "none"
    c._is_byo = is_byo
    return c


class TestCallFallsBackToLocal:
    def test_local_answers_when_cloud_providers_fail(self):
        local = _provider("Local (local-fast)")
        local.ask.return_value = "local answer"
        c = _client(
            _provider("Groq", ask=Exception("404 model_not_found")),
            _provider("Gemini", ask=Exception("API key not valid")),
            local,
        )
        assert c.ask("q") == "local answer"
        assert c.provider == "Local (local-fast)"

    def test_local_not_called_when_primary_succeeds(self):
        primary = _provider("Groq")
        primary.ask.return_value = "cloud"
        local = _provider("Local (local-fast)")
        assert _client(primary, None, local).ask("q") == "cloud"
        local.ask.assert_not_called()

    def test_local_tried_once_per_call_across_retries(self):
        local = _provider("Local (local-fast)", ask=Exception("Connection error."))
        c = _client(_provider("Groq", ask=Exception("429 rate limit")), None, local)
        with pytest.raises(LLMProvidersExhausted):
            c.ask("q")
        assert local.ask.call_count == 1

    def test_rate_limited_cloud_falls_through_to_local_on_first_attempt(self):
        groq = _provider("Groq", ask=Exception("429 Too Many Requests"))
        local = _provider("Local (local-fast)")
        local.ask.return_value = "ok"
        assert _client(groq, None, local).ask("q") == "ok"
        assert groq.ask.call_count == 1

    def test_byo_failure_falls_back_to_local(self):
        local = _provider("Local (local-fast)")
        local.ask.return_value = "ok"
        c = _client(_provider("gpt-x", ask=Exception("401 unauthorized")), None, local, is_byo=True)
        assert c.ask("q") == "ok"


class TestStreamFallsBackToLocal:
    def test_stream_uses_local_when_cloud_fails_before_output(self):
        local = _provider("Local (local-fast)")
        local.stream.return_value = iter(["he", "llo"])
        c = _client(_provider("Groq", stream=Exception("404 model_not_found")), None, local)
        assert "".join(c.stream("q")) == "hello"

    def test_local_context_overflow_reported_as_too_large(self):
        err = Exception("request (9030 tokens) exceeds the available context size (8192 tokens)")
        c = _client(
            _provider("Groq", stream=Exception("413 Request too large")),
            None,
            _provider("Local (local-fast)", stream=err),
        )
        with pytest.raises(LLMProvidersExhausted) as exc:
            list(c.stream("q"))
        assert exc.value.too_large


class TestClassifyLlamaCppErrors:
    def test_context_size_error_is_too_large(self):
        msg = "Error code: 400 - {'message': 'request (9030 tokens) exceeds the available context size'"
        assert _classify_error(Exception(msg + ", 'type': 'exceed_context_size_error'}")) == "too_large"


class TestInit:
    def test_disabled_when_base_url_unset(self, monkeypatch):
        monkeypatch.setattr(local_llm_module, "LOCAL_LLM_BASE_URL", "")
        c = LLMClient.__new__(LLMClient)
        c._system = "s"
        assert c._init_local() is None

    def test_trial_path_runs_on_local_alone_without_cloud_keys(self, monkeypatch, local_on):
        monkeypatch.setattr("agents.llm_client._groq_rotator.available", lambda: False)
        monkeypatch.setattr(llm_client_module, "GEMINI_API_KEY", "")
        c = LLMClient("s")
        assert c._primary is None and c._local is not None
        assert c.provider.startswith("Local")

    def test_no_providers_at_all_still_raises(self, monkeypatch):
        monkeypatch.setattr(local_llm_module, "LOCAL_LLM_BASE_URL", "")
        monkeypatch.setattr("agents.llm_client._groq_rotator.available", lambda: False)
        monkeypatch.setattr(llm_client_module, "GEMINI_API_KEY", "")
        with pytest.raises(ValueError, match="No LLM provider configured"):
            LLMClient("s")


class TestLocalLLMProvider:
    def test_localhost_allowed_because_url_is_operator_config(self, local_on):
        p = LocalLLMProvider("s")
        assert str(p._client.base_url).startswith("http://127.0.0.1:8080")
        assert p._client.max_retries == 0

    def test_non_http_scheme_rejected(self, monkeypatch):
        monkeypatch.setattr(local_llm_module, "LOCAL_LLM_BASE_URL", "file:///etc/passwd")
        with pytest.raises(ValueError, match="http"):
            LocalLLMProvider("s")

    def test_output_capped_and_model_override_ignored(self, monkeypatch, local_on):
        monkeypatch.setattr(local_llm_module, "LOCAL_LLM_MAX_TOKENS", 256)
        p = LocalLLMProvider("s")
        fake = MagicMock()
        fake.chat.completions.create.return_value.choices = [MagicMock(message=MagicMock(content=" hi "))]
        p._client = fake
        assert p.ask("q", max_tokens=2000, model="llama-3.1-8b-instant") == "hi"
        kwargs = fake.chat.completions.create.call_args.kwargs
        assert kwargs["max_tokens"] == 256
        assert kwargs["model"] == local_llm_module.LOCAL_LLM_MODEL
