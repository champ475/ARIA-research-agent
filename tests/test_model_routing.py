"""LLM-08: per-node model routes from config, retired models skipped with a
warning, and a model change needs no code change."""

from __future__ import annotations

import pytest

import config
from agents import llm_client as lc


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(lc._groq_rotator, "available", lambda: True)
    monkeypatch.setattr(lc, "GEMINI_API_KEY", "test-key")
    lc._provider_health._unavailable_until.clear()
    yield
    lc._provider_health._unavailable_until.clear()


class _FakeProvider:
    def __init__(self, provider, system, model=None):
        self.name, self.model = provider, model
        self.health_key = f"{provider}:{model}"
        self.calls = 0

    def ask(self, prompt, temperature, max_tokens, model):
        self.calls += 1
        if "retired" in self.model:
            raise RuntimeError("Error code: 404 - The model `retired-model` has been decommissioned")
        return f"answer from {self.model}"


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(lc, "_GroqProvider", lambda system, model=None: _FakeProvider("groq", system, model))
    monkeypatch.setattr(
        lc, "_GeminiProvider", lambda system, model=None: _FakeProvider("gemini", system, model)
    )
    monkeypatch.setattr(lc.LLMClient, "_init_local", lambda self: None)


def test_routes_come_from_config_and_env(monkeypatch):
    assert config.model_route("planner")[0] == ("groq", config.GROQ_MODEL_FAST)
    assert config.model_route("communication")[0] == ("groq", config.GROQ_MODEL)
    monkeypatch.setenv("ARIA_MODEL_PLANNER", "gemini:gemini-x, groq:new-model, bogus:entry")
    assert config.model_route("planner") == [("gemini", "gemini-x"), ("groq", "new-model")]
    monkeypatch.setenv("ARIA_MODELS_FAST", "groq:fast-a")
    assert config.model_route("reflection") == [("groq", "fast-a")]


def test_client_follows_the_node_route(fakes, monkeypatch):
    monkeypatch.setenv("ARIA_MODEL_PLANNER", "groq:m1,gemini:m2,groq:m3")
    client = lc.LLMClient("sys", node="planner")
    assert [p.model for p in client._chain()] == ["m1", "m2", "m3"]
    assert client.ask("q") == "answer from m1"


def test_decommissioned_model_warns_and_falls_back(fakes, monkeypatch, caplog):
    monkeypatch.setenv("ARIA_MODEL_PLANNER", "groq:retired-model,gemini:good-model")
    client = lc.LLMClient("sys", node="planner")
    assert client.ask("q") == "answer from good-model"
    assert lc._provider_health.is_unavailable("groq:retired-model")
    # A later client doesn't even build the retired entry.
    later = lc.LLMClient("sys", node="planner")
    assert [p.model for p in later._chain()] == ["good-model"]
    assert any("model_gone" in r.getMessage() for r in caplog.records)


def test_startup_check_marks_retired_models(fakes, monkeypatch):
    from agents import model_health

    monkeypatch.setattr(
        config, "all_routed_models", lambda: [("groq", "retired-model"), ("gemini", "good-model")]
    )
    results = model_health.check_models()
    assert results == {"groq:retired-model": "model_gone", "gemini:good-model": "ok"}
    assert lc._provider_health.is_unavailable("groq:retired-model")


def test_retired_model_on_one_provider_keeps_its_other_models(fakes, monkeypatch):
    lc._provider_health.mark_unavailable("groq:retired-model")
    monkeypatch.setenv("ARIA_MODEL_PLANNER", "groq:retired-model,groq:other-model")
    assert [p.model for p in lc.LLMClient("sys", node="planner")._chain()] == ["other-model"]


def test_classifier_recognises_retirement_messages():
    for msg in (
        "The model `llama-3.1-8b-instant` has been decommissioned",
        "404 models/gemini-2.0-flash is not found for API version v1beta",
        "model_not_found",
    ):
        assert lc._classify_error(RuntimeError(msg)) == "model_gone"


def test_no_hardcoded_model_override_in_nodes():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in (root / "graph").rglob("*.py"):
        assert "GROQ_MODEL_FAST" not in path.read_text(encoding="utf-8"), path
