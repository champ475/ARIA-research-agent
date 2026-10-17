"""LLM-02: the answer prompt carries the actual rows, headlines and passages
behind the conclusions, within a fixed budget."""

import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from graph.evidence_render import render_evidence_block, render_for_prompt
from graph.nodes.communication_layer import build_communication_layer_node
from graph.nodes.execute_task import _to_cacheable


def _ev(etype, payload, entity="TCS"):
    return {
        "id": f"{etype}-{entity}",
        "entity": entity,
        "evidence_type": etype,
        "payload": payload,
        "tool": etype,
    }


FUNDS = pd.DataFrame(
    {
        "scheme_code": [1, 2, 3],
        "scheme_name": [
            "Axis ELSS Tax Saver Direct Growth",
            "Mirae ELSS Tax Saver Direct Growth",
            "DSP ELSS Direct Growth",
        ],
        "nav": [92.4511, 48.12, 110.0],
        "nav_date": ["13-Oct-2026"] * 3,
        "amc": ["Axis", "Mirae", "DSP"],
    }
)
NEWS = [
    {"title": "TCS shares fall 4% after Q2 miss", "source": "ET", "published": "Tue, 14 Oct 2026 09:10"},
    {"title": "TCS shares fall 4% after Q2 miss", "source": "Mint", "published": "Tue, 14 Oct 2026 09:30"},
    {"title": "TCS wins $1bn UK deal", "source": "BS", "published": "Mon, 13 Oct 2026"},
] + [{"title": f"Headline {i}", "source": "X", "published": ""} for i in range(20)]
FILING = (
    "Cover page and boilerplate. "
    * 30
    + "Statement of Profit and Loss for the quarter ended 30 Sep 2026: Revenue from operations 64,259 crore; "
    "Net profit 11,909 crore. "
    + "Filler text. " * 50
    + "Segment information: BFSI 31.2% of revenue, growth 2.1%. "
)


def test_fund_table_keeps_names_and_navs_verbatim():
    text = render_for_prompt(_ev("mf_category_funds", _to_cacheable(FUNDS), "ELSS"), 2000)
    assert "Axis ELSS Tax Saver Direct Growth" in text and "92.45" in text and "| nav |" in text.lower()


def test_news_is_deduplicated_capped_and_sourced():
    text = render_for_prompt(_ev("news_headline", NEWS), 5000)
    assert text.count("TCS shares fall 4% after Q2 miss") == 1
    assert "(ET, Tue, 14 Oct 2026)" in text  # date only; times are noise
    assert len(text.splitlines()) == 8


def test_filing_picks_the_passages_that_matter():
    text = render_for_prompt(_ev("pdf_extract", FILING), 900)
    assert "64,259" in text and "Net profit 11,909" in text and "BFSI 31.2%" in text
    assert len(text) <= 900


def test_movers_and_deals_render_as_tables():
    movers = pd.DataFrame(
        {"symbol": ["UP1", "UP2"], "ltp": [101.5, 55.0], "pChange": [5.1, 4.2], "junk": ["x", "y"]}
    )
    text = render_for_prompt(_ev("top_movers", _to_cacheable(movers), "market"), 1000)
    assert "UP1" in text and "5.1" in text and "junk" not in text


def test_third_party_text_is_fenced_and_budget_respected():
    block = render_evidence_block(
        [_ev("news_headline", NEWS), _ev("mf_category_funds", _to_cacheable(FUNDS), "ELSS")],
        3000,
        {"mf"},
        "best elss",
    )
    assert '<untrusted source="news_headline">' in block
    assert "Axis ELSS" in block and len(block) <= 3000


def test_numbers_only_evidence_adds_nothing():
    assert render_for_prompt(_ev("fundamental_ratio", {"pe": 24.3}), 1000) == ""


@settings(max_examples=60, deadline=None)
@given(
    budget=st.integers(min_value=0, max_value=6000),
    n_news=st.integers(min_value=0, max_value=40),
    n_rows=st.integers(min_value=0, max_value=60),
    title=st.text(min_size=0, max_size=400),
)
def test_block_never_exceeds_its_budget(budget, n_news, n_rows, title):
    news = [{"title": f"{title} {i}", "source": "S", "published": "d"} for i in range(n_news)]
    df = pd.DataFrame(
        {
            "scheme_name": [f"{title[:50]} {i}" for i in range(n_rows)],
            "nav": [float(i) for i in range(n_rows)],
        }
    )
    evidence = [
        _ev("news_headline", news),
        _ev("mf_category_funds", _to_cacheable(df), "X"),
        _ev("pdf_extract", title * 20),
    ]
    assert len(render_evidence_block(evidence, budget, {"mf"}, "why")) <= budget


class _Capture:
    def __init__(self):
        self.prompt = None

    def ask(self, prompt, **kwargs):
        self.prompt = prompt
        return "answer"


def _conclusion(claim):
    return {"claim": claim, "value": 1, "confidence": 1.0, "evidence_ids": ["e"], "citation_key": "x"}


def test_mf_question_prompt_contains_the_fund_names():
    client = _Capture()
    node = build_communication_layer_node(client)
    node(
        {
            "intent": "mf",
            "query": "best ELSS funds",
            "objective": "best ELSS funds",
            "conclusions": [_conclusion("ELSS: 3 matching mutual fund(s) found")],
            "evidence": [_ev("mf_category_funds", _to_cacheable(FUNDS), "ELSS")],
            "entities": [],
        }
    )
    assert "Mirae ELSS Tax Saver Direct Growth" in client.prompt and "Evidence data:" in client.prompt


def test_why_question_prompt_contains_the_headlines():
    node = build_communication_layer_node(_Capture())
    out = node(
        {
            "intent": "equity",
            "query": "why is TCS down today",
            "objective": "why is TCS down",
            "conclusions": [_conclusion("TCS has 23 recent news headline(s) on record")],
            "evidence": [_ev("news_headline", NEWS)],
            "entities": ["TCS"],
        }
    )
    assert "TCS wins $1bn UK deal" in out["pending_synthesis_prompt"]


def test_movers_table_shows_the_percent_change_column():
    # NSE spells it perChange; without it the writer had only prices (smoke test 2026-10-17).
    df = pd.DataFrame({"symbol": ["TECHM", "TCS"], "ltp": [1790, 2465], "perChange": [4.21, 3.9]})
    text = render_for_prompt(_ev("top_movers", df, "market"), 2000)
    assert "perChange" in text and "4.21" in text
