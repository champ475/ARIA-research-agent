"""
Covers the model-override addition to LLMClient (agents/llm_client.py), which
the graph work needs to route QueryAnalyzer/Reflection/Verification's cheap
calls through GROQ_MODEL_FAST instead of the primary model (RFC Sec17).
"""
from unittest.mock import MagicMock, patch

import pytest

from agents.llm_client import LLMClient, _GroqProvider
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
