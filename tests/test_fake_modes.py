"""TEST-04: the deterministic LLM and offline HTTP used by e2e runs and
demos — predictable, grounded, never in production."""

from __future__ import annotations

import json

import pytest

from agents.providers.fake_llm import FakeLLMProvider, classify


@pytest.mark.parametrize(
    "query, intent",
    [
        ("Analyse TCS", "equity"),
        ("Alert me if INFY falls 5%", "alert"),
        ("Best ELSS for tax saving", "mf"),
        ("IT stocks with PE below 25", "screener"),
        ("IC report for TCS", "report"),
        ("I'm 30 and want to plan for retirement", "planning"),
        ("hello", "chat"),
    ],
)
def test_classification(query, intent):
    assert classify(query, has_company=intent in ("equity", "report", "alert"))["intent"] == intent


def test_nodes_answer_in_their_schema_and_answers_are_grounded():
    from graph.grounding import check

    qa = FakeLLMProvider(node="query_analyzer").ask("Detected entities: TCS\nUser query: Analyse TCS")
    assert json.loads(qa)["intent"] == "equity"
    plan = FakeLLMProvider(node="planner").ask(
        "Objective: alert me if TCS drops\n\nAvailable tools:\n- alert_create: x"
    )
    assert json.loads(plan) == {"tools": ["alert_create"]}
    intake = FakeLLMProvider(node="communication").ask(
        "USER'S LATEST MESSAGE: \"I'm 30, 10 lakh, retirement in 20 years, moderate\""
    )
    assert json.loads(intake)["updates"] == {
        "age": 30,
        "capital": 1000000.0,
        "horizon_years": 20,
        "goal": "retirement",
        "risk_tolerance": "moderate",
    }
    prompt = "Objective: x\n\nVerified conclusions:\n- TCS P/E = 25.4 (confidence: 1.00)"
    answer = FakeLLMProvider(node="communication").ask(prompt)
    assert "TCS P/E = 25.4" in answer and check(answer, prompt).ok
    assert "".join(FakeLLMProvider(node="communication").stream(prompt)) == answer


def test_llm_client_uses_it_when_enabled(monkeypatch):
    from agents.llm_client import LLMClient

    monkeypatch.setattr("config.ARIA_FAKE_LLM", True)
    client = LLMClient("system", node="planner")
    assert client.provider == "fake"
    assert json.loads(client.ask("Objective: analyse TCS\n\nAvailable tools:\n- fetch_info: x"))["tools"] == [
        "fetch_info"
    ]


def test_offline_http(monkeypatch):
    from utils import http

    monkeypatch.setattr("config.ARIA_FAKE_DATA", True)
    nav = http.get("https://portal.amfiindia.com/spages/NAVAll.txt", source="amfi")
    assert nav.status_code == 200 and nav.text.startswith("Scheme Code")
    assert http.get("https://example.com/anything", source="x").status_code == 503


def test_never_in_production():
    from config import Settings, boot_problems

    s = Settings(APP_ENV="prod", DATABASE_URL="postgresql://u:p@h/db", ARIA_FAKE_LLM=True)
    assert any("ARIA_FAKE_LLM" in p for p in boot_problems(s, serving=False))
