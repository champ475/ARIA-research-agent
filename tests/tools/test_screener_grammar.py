"""VAL-06: screener filter grammar — 50 phrasings, contradictions, the
"Filters applied / Not understood" echo, and filters actually applied."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tools.screener import as_screen, run, to_markdown
from tools.screener_grammar import parse

# (query, expected filters as (field, op, value[, high]), sector)
CASES = [
    ("PE below 20", [("pe", "<", 20)], None),
    ("P/E <= 18.5", [("pe", "<=", 18.5)], None),
    ("p/e < 15", [("pe", "<", 15)], None),
    ("pe ratio under 25", [("pe", "<", 25)], None),
    ("price to earnings less than 30", [("pe", "<", 30)], None),
    ("PE above 40", [("pe", ">", 40)], None),
    ("PE at least 10", [("pe", ">=", 10)], None),
    ("PE between 10 and 20", [("pe", "between", 10, 20)], None),
    ("PE from 12 to 18", [("pe", "between", 12, 18)], None),
    ("PE 10-20", [("pe", "between", 10, 20)], None),
    ("PE of at most 22.5", [("pe", "<=", 22.5)], None),
    ("P/B below 2", [("pb", "<", 2)], None),
    ("price to book under 1.5", [("pb", "<", 1.5)], None),
    ("PB >= 3", [("pb", ">=", 3)], None),
    ("ROE > 15%", [("roe", ">", 15)], None),
    ("ROE above 15", [("roe", ">", 15)], None),
    ("return on equity at least 18 percent", [("roe", ">=", 18)], None),
    ("ROCE over 20%", [("roce", ">", 20)], None),
    ("return on capital employed more than 25", [("roce", ">", 25)], None),
    ("D/E < 0.5", [("de", "<", 0.5)], None),
    ("debt to equity below 1", [("de", "<", 1)], None),
    ("debt/equity at most 0.3", [("de", "<=", 0.3)], None),
    ("market cap above 50,000 cr", [("mcap", ">", 50000)], None),
    ("market cap > 1 lakh crore", [("mcap", ">", 100000)], None),
    ("mcap under 5000", [("mcap", "<", 5000)], None),
    ("market capitalisation between 10,000 and 50,000 crore", [("mcap", "between", 10000, 50000)], None),
    ("dividend yield of at least 3%", [("div_yield", ">=", 3)], None),
    ("div yield above 2", [("div_yield", ">", 2)], None),
    ("revenue growth more than 12 percent", [("rev_growth", ">", 12)], None),
    ("sales growth >= 20%", [("rev_growth", ">=", 20)], None),
    ("stocks trading below 500", [("price", "<", 500)], None),
    ("share price above ₹1,000", [("price", ">", 1000)], None),
    ("near 52 week high", [("from_52w_high", "<=", 5)], None),
    ("close to their 52-week low", [("from_52w_low", "<=", 5)], None),
    ("IT stocks with PE below 30", [("pe", "<", 30)], "Technology"),
    ("banks with PB below 2", [("pb", "<", 2)], "Financial Services"),
    ("pharma stocks with ROE above 20", [("roe", ">", 20)], "Healthcare"),
    ("auto stocks with D/E < 0.5", [("de", "<", 0.5)], "Consumer Cyclical"),
    ("FMCG with PE under 50", [("pe", "<", 50)], "Consumer Defensive"),
    ("metal stocks with PE below 10", [("pe", "<", 10)], "Metals & Mining"),
    ("telecom companies with ROCE above 10", [("roce", ">", 10)], "Telecom"),
    ("realty stocks with D/E below 1", [("de", "<", 1)], "Real Estate"),
    ("large cap stocks with PE under 25", [("mcap", ">=", 20000), ("pe", "<", 25)], None),
    ("mid cap with ROE above 15%", [("mcap", ">=", 5000), ("mcap", "<=", 20000), ("roe", ">", 15)], None),
    ("small caps with D/E below 0.5", [("mcap", "<=", 5000), ("de", "<", 0.5)], None),
    ("IT stocks with P/E <= 18.5 and ROE > 15%", [("pe", "<=", 18.5), ("roe", ">", 15)], "Technology"),
    ("PE < 20, ROCE > 15 and D/E < 1", [("pe", "<", 20), ("roce", ">", 15), ("de", "<", 1)], None),
    (
        "banks with PE between 8 and 15 and dividend yield above 1.5%",
        [("pe", "between", 8, 15), ("div_yield", ">", 1.5)],
        "Financial Services",
    ),
    ("stocks with ROE above 15 with something weird", [("roe", ">", 15)], None),
    ("show me companies with high growth", [], None),
]


def _tuples(screen):
    out = []
    for f in screen.filters:
        t = (f.field, f.op, f.value) if f.op != "between" else (f.field, f.op, f.value, f.high)
        out.append(t)
    return out


@pytest.mark.parametrize(("query", "expected", "sector"), CASES)
def test_phrasings(query, expected, sector):
    s = parse(query)
    assert _tuples(s) == [tuple(float(x) if isinstance(x, (int, float)) else x for x in e) for e in expected]
    assert s.sector == sector


def test_case_count():
    assert len(CASES) >= 50


def test_with_never_reads_as_it():
    """The old substring match made every "... with ..." query an IT screen."""
    assert parse("banks with PB below 2").sector == "Financial Services"
    assert parse("stocks with PE below 20").sector is None


def test_unrecognised_fragments_are_reported():
    s = parse("stocks with ROE above 15 with something weird")
    assert s.not_understood == ["something weird"]
    assert parse("show me companies with high growth").not_understood == ["high growth"]
    assert parse("low PE stocks").not_understood == ["low PE"]


@pytest.mark.parametrize(
    "query",
    ["PE below 10 and PE above 20", "ROE < 5 and ROE > 5", "PE between 10 and 20 and PE above 25"],
)
def test_contradictions_are_detected(query):
    s = parse(query)
    assert s.contradictions
    assert run(s.as_dict()) == []
    assert "can't" in to_markdown([], s.as_dict()).lower() or "No stock can match" in to_markdown(
        [], s.as_dict()
    )


def test_reply_always_lists_applied_filters():
    s = parse("IT stocks with PE below 30 and something odd").as_dict()
    md = to_markdown([], s)
    assert "**Filters applied:** sector: Technology; P/E < 30" in md
    assert "**Not understood (ignored):** 'something odd'" in md


def _info(pe, roe, mcap_cr, sector="Technology", de=30.0):
    return {
        "trailingPE": pe,
        "returnOnEquity": roe / 100,
        "marketCap": mcap_cr * 1e7,
        "sector": sector,
        "debtToEquity": de,
        "currentPrice": 100.0,
        "shortName": "X",
    }


def test_run_applies_every_filter_and_unknown_metrics_fail():
    infos = {
        "AAA": _info(15, 20, 30000),
        "BBB": _info(35, 25, 30000),  # PE too high
        "CCC": _info(12, 10, 30000),  # ROE too low
        "DDD": {"sector": "Technology", "currentPrice": 1.0, "marketCap": 3e11},  # no PE: excluded
    }
    with (
        patch("tools.screener.get_info", side_effect=lambda s: infos.get(s, {})),
        patch("tools.screener.STOCK_UNIVERSE", list(infos)),
    ):
        out = run(parse("PE below 20 and ROE above 15").as_dict())
    assert [r["symbol"] for r in out] == ["AAA"]


def test_legacy_flat_filters_still_work():
    s = as_screen({"max_pe": 20, "min_mktcap_cr": 5000})
    assert _tuples(s) == [("pe", "<=", 20.0), ("mcap", ">=", 5000.0)]


@settings(max_examples=500, deadline=None)
@given(st.text())
def test_parser_never_raises(text):
    s = parse(text)
    s.summary()
    as_screen(s.as_dict())


class _Stub:
    def __init__(self, reply):
        self.reply = reply

    def ask(self, prompt, **kwargs):
        return self.reply


def test_llm_fallback_only_when_grammar_finds_nothing_and_output_is_validated():
    import json

    from graph import tool_catalogue as tc

    reply = json.dumps(
        {
            "filters": [
                {"field": "pe", "op": "<", "value": 15, "high": None},
                {"field": "__import__", "op": "<", "value": 1},  # unknown field: dropped
                {"field": "roe", "op": "~", "value": 5},  # unknown operator: dropped
                {"field": "pb", "op": "between", "value": 1},  # between without high: dropped
            ],
            "sector": "Not A Sector",
        }
    )
    with patch.object(tc, "_get_extract_client", return_value=_Stub(reply)) as client:
        out = tc._screener_parse_query("market", {"_query": "cheap valuations please"})
        assert out["source"] == "llm" and out["sector"] is None
        assert out["filters"] == [{"field": "pe", "op": "<", "value": 15.0, "high": None}]
        grammar = tc._screener_parse_query("market", {"_query": "PE below 20"})
        assert grammar["source"] == "grammar"
        assert client.call_count == 1  # the grammar's own result needed no LLM
