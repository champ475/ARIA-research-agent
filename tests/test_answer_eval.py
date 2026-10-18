"""EVAL-01: the answer-level eval's cases, scorer and replay seams."""

import json
from pathlib import Path

import pytest

from evals.answers import build_cases, score
from evals.answers.fixture_provider import FixtureProvider
from evals.answers.replay import ReplayProvider, ReplayStore
from evals.runner import MissingFixture

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    json.loads(line) for line in (ROOT / "evals/answers/cases.jsonl").read_text(encoding="utf-8").splitlines()
]


def test_case_file_is_up_to_date():
    expected = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in build_cases.CASES)
    assert (ROOT / "evals/answers/cases.jsonl").read_text(encoding="utf-8") == expected


def test_cases_cover_every_family_and_the_review_probes():
    assert len(CASES) >= 60
    assert len({c["id"] for c in CASES}) == len(CASES)
    families = {c["family"] for c in CASES}
    assert families >= {
        "equity",
        "compare",
        "peers",
        "concept",
        "tax",
        "mf",
        "screener",
        "alerts",
        "followup",
        "advice",
        "backtest",
    }
    assert sum(len(c["turns"]) for c in CASES if c["probe"]) >= 29
    assert all(any(t.get("followup") for t in c["turns"]) for c in CASES if c["family"] == "followup")


def test_reference_facts_come_from_the_frozen_fixtures():
    itc = next(c for c in CASES if c["id"] == "eq-itc-div")
    assert itc["turns"][0]["facts"][0]["value"] == pytest.approx(6.07)


def _turn(text, misses=()):
    return {"text": text, "misses": list(misses)}


def _case(family="equity", **spec):
    return {"id": "x", "family": family, "turns": [{"q": "q", **spec}]}


DIV = build_cases.fact("ITC", "dividendYield", False)


@pytest.mark.parametrize(
    "text, wrong",
    [
        ("ITC's dividend yield is 6.07%.", 0),
        ("ITC's dividend yield is about 6.1%.", 0),
        ("ITC’s dividend yield is 0.0% [^].", 1),
        ("ITC pays a dividend of ₹16 a share.", 0),  # a different metric: not checked
        ("| Metric | Value |\n|---|---|\n| Dividend yield | 0.0% |", 0),  # metric rows need an entity
    ],
)
def test_single_company_fact(text, wrong):
    case = _case(facts=[DIV])
    assert len(score.score_turn(case, case["turns"][0], _turn(text))["wrong"]) == wrong


def test_table_and_transposed_table_facts():
    facts = [build_cases.fact(s, "dividendYield") for s in ("TCS", "INFY")]
    case = _case("compare", facts=facts)
    table = (
        "| Company | P/E | Div. yield |\n|---|---|---|\n| TCS | 17.0 | 1.95% |\n| Infosys | 15.7 | 0.05% |"
    )
    s = score.score_turn(case, case["turns"][0], _turn(table))
    assert s["facts_found"] == 2 and len(s["wrong"]) == 1 and "Infosys" in s["wrong"][0]
    transposed = "| Metric | TCS | Infosys |\n|---|---|---|\n| Dividend yield | 1.95% | 4.31% |"
    s = score.score_turn(case, case["turns"][0], _turn(transposed))
    assert s["facts_found"] == 2 and not s["wrong"]


def test_two_companies_in_one_sentence_read_their_own_figure():
    facts = [build_cases.fact(s, "trailingPE") for s in ("TCS", "INFY")]
    case = _case("compare", facts=facts)
    text = "TCS trades at a P/E of 17.0 while Infosys is at 15.7."
    assert not score.score_turn(case, case["turns"][0], _turn(text))["wrong"]


@pytest.mark.parametrize(
    "text, breach",
    [
        ("SBI has room for upside.", True),
        ("The stock looks bullish.", True),
        ("Analysts see 38% upside to the mean target.", True),
        ("These are suitable candidates for a long-term portfolio.", True),
        ("MACD gave a bullish crossover last week.", False),
        ("This is not a recommendation to buy; it is not suitable for every portfolio.", False),
        ("ℹ️ *ARIA doesn't tell you what to buy or sell.*", False),
        ("The analyst mean target is ₹1,666, 38% above the last price.", False),
    ],
)
def test_advice_breaches(text, breach):
    assert bool(score.advice_breaches(text)) is breach


def test_notes_are_detected_by_kind():
    text = "x\n\n⚠️ *Unverified figures: 4.54 % — …*\n\n⚠️ *Could not fully confirm: SBI P/E.*"
    assert score.notes(text) == ["unverified", "unconfirmed"]
    assert score.notes("🕒 *Data as of 15 Oct 2026*") == []


def test_gate_and_regressions():
    report = {"metrics": {k: 0 for k in score.COUNTS} | {"note_rate": 9.0, "fact_coverage": 90.0}}
    assert score.gate_problems(report) == []
    report["metrics"]["wrong_figures"] = 1
    report["metrics"]["note_rate"] = 12.0
    assert len(score.gate_problems(report)) == 2
    base = {"metrics": {"wrong_figures": 0, "note_rate": 8.0, "fact_coverage": 95.0}}
    assert len(score.regressions(report, base)) == 3


def test_a_missing_fixture_fails_the_answer():
    case = _case()
    s = score.score_turn(case, case["turns"][0], _turn("", misses=["planner:abc"]))
    assert s["missing"]
    assert (
        score.report([case], [{"id": "x", "turns": [_turn("", ["planner:abc"])]}])["metrics"][
            "missing_fixtures"
        ]
        == 1
    )


def test_replay_provider_replays_and_refuses_unknown_prompts(tmp_path):
    store = ReplayStore(tmp_path / "f.jsonl", "replay")
    provider = ReplayProvider("sys", "communication", store)
    store.data[provider._key("hello")] = "a recorded answer that is longer than one chunk of forty characters"
    assert provider.ask("hello").startswith("a recorded")
    assert "".join(provider.stream("hello")) == store.data[provider._key("hello")]
    with pytest.raises(MissingFixture):
        provider.ask("never recorded")
    assert store.misses == [f"communication:{provider._key('never recorded')[:12]}"]


def test_llm_client_uses_the_installed_override():
    from agents import llm_client

    llm_client.set_provider_override(
        lambda system, node: ReplayProvider(system, node, ReplayStore(Path("x"), "replay"))
    )
    try:
        client = llm_client.LLMClient("sys", node="planner")
        assert client.provider == "replay"
    finally:
        llm_client.set_provider_override(None)
    assert llm_client.LLMClient.live is not None


def test_replayable_evidence_ids_depend_only_on_what_was_fetched(monkeypatch):
    from datetime import UTC, datetime

    from graph.nodes import execute_task

    monkeypatch.setattr(execute_task, "REPLAYABLE", True)
    monkeypatch.setattr(execute_task, "PINNED_NOW", datetime(2026, 10, 15, tzinfo=UTC))
    a = execute_task._evidence_id("fetch_info", "ITC", "quote_info", "k")
    assert a == execute_task._evidence_id("fetch_info", "ITC", "quote_info", "k")
    assert a != execute_task._evidence_id("fetch_info", "ITC", "quote_info", "other")
    assert execute_task._now() == datetime(2026, 10, 15, tzinfo=UTC)


def test_fixture_provider_serves_the_frozen_quote_and_statements():
    p = FixtureProvider()
    q = p.quote("ITC")
    assert q["dividendYield"] == pytest.approx(6.07)
    assert q["currentPrice"] == q["regularMarketPrice"]
    assert q["dayLow"] < q["currentPrice"] < q["dayHigh"] * 1.02
    assert not p.financials("TCS")["income_annual"].empty
    assert p.quote("NOTAFIXTURE")["shortName"] == "NOTAFIXTURE"  # falls back to fake data
