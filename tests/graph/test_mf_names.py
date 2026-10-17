"""WP5 (smoke test 2026-10-17): the fund comparison searched for a fund called "market"."""

import pytest

from graph.entity_resolution import extract_fund_names
from graph.tool_catalogue import NO_ENTITY_SENTINEL, get_tool


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Compare Parag Parikh Flexi Cap and HDFC Flexi Cap", ["Parag Parikh Flexi Cap", "HDFC Flexi Cap"]),
        ("Parag Parikh Flexi Cap fund returns", ["Parag Parikh Flexi Cap"]),
        ("Axis Bluechip vs Mirae Asset Large Cap Fund?", ["Axis Bluechip", "Mirae Asset Large Cap"]),
        (
            "Which is better, SBI Small Cap or Nippon India Small Cap?",
            ["SBI Small Cap", "Nippon India Small Cap"],
        ),
        ("How is HDFC Flexi Cap doing today", ["HDFC Flexi Cap"]),
        ("What is the NAV of Parag Parikh Flexi Cap fund", ["Parag Parikh Flexi Cap"]),
        ("returns of Axis Bluechip", ["Axis Bluechip"]),
        (
            "Compare ICICI Prudential Bluechip, Axis Bluechip and SBI Bluechip",
            ["ICICI Prudential Bluechip", "Axis Bluechip", "SBI Bluechip"],
        ),
        ("", []),
    ],
)
def test_extract_fund_names(query, expected):
    assert extract_fund_names(query) == expected


def test_comparison_uses_the_names_from_the_question_not_the_sentinel(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "graph.tool_catalogue.get_mf_comparison", lambda names: seen.setdefault("names", names) and "table"
    )
    out = get_tool("fetch_mf_comparison").fn(
        NO_ENTITY_SENTINEL, {"_query": "Compare Parag Parikh Flexi Cap and HDFC Flexi Cap"}
    )
    assert out == "table"
    assert seen["names"] == ["Parag Parikh Flexi Cap", "HDFC Flexi Cap"]


def test_comparison_without_any_fund_name_says_so(monkeypatch):
    monkeypatch.setattr(
        "graph.tool_catalogue.get_mf_comparison", lambda names: pytest.fail("must not search")
    )
    out = get_tool("fetch_mf_comparison").fn(NO_ENTITY_SENTINEL, {"_query": ""})
    assert out.lower().startswith("no ")  # a "nothing found" text never becomes a conclusion


def test_category_tool_reads_the_users_words_for_the_sentinel(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "graph.tool_catalogue.get_category_funds", lambda kw, top_n=10: seen.setdefault("kw", kw)
    )
    get_tool("fetch_category_funds").fn(NO_ENTITY_SENTINEL, {"_query": "best ELSS funds"})
    assert seen["kw"] == "best ELSS funds"
    seen.clear()
    get_tool("fetch_category_funds").fn("elss", {"_query": "something else"})
    assert seen["kw"] == "elss"


def test_nav_search_uses_the_first_named_fund(monkeypatch):
    seen = {}
    monkeypatch.setattr("graph.tool_catalogue.get_all_nav", lambda: "nav-table")
    monkeypatch.setattr("graph.tool_catalogue.search_fund", lambda q, df: seen.update(q=q, df=df) or "rows")
    get_tool("fetch_nav").fn(NO_ENTITY_SENTINEL, {"_query": "NAV of Parag Parikh Flexi Cap fund"})
    assert seen["q"] == "Parag Parikh Flexi Cap"


def test_fund_tools_are_cached_per_fund_not_per_sentinel():
    spec = get_tool("fetch_mf_comparison")
    a = spec.cache_key(NO_ENTITY_SENTINEL, {"_query": "Compare Axis Bluechip and SBI Bluechip"})
    b = spec.cache_key(NO_ENTITY_SENTINEL, {"_query": "Compare Parag Parikh Flexi Cap and HDFC Flexi Cap"})
    assert a != b
    assert a == spec.cache_key(NO_ENTITY_SENTINEL, {"_query": "compare AXIS BLUECHIP and sbi bluechip"})
