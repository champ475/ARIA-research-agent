"""
evals/build_judge_labels.py — authoring source for evals/cases/judge_labels.jsonl (LLM-13).

100 labelled (objective, evidence summary) pairs for the verification judge:
50 where the evidence is enough to answer ("sufficient": the judge should
raise nothing) and 50 where it is not ("insufficient": the judge should raise
at least one issue). They are synthetic but shaped exactly like
EvidenceSummarizer output, and the insufficient ones fall in five families:

  wrong_entity   evidence is about another company
  wrong_type     evidence kind doesn't match what was asked (news for a technicals question)
  missing_leg    a comparison has evidence for only one side
  empty          nothing was collected, or a collected frame/list is empty
  low_signal     the only evidence is a bare quote for an analysis question

Re-run after editing:  python evals/build_judge_labels.py
Score a judge with:    python evals/judge_calibration.py [--record]
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "cases" / "judge_labels.jsonl"

COMPANIES = [
    "TCS", "INFY", "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "ITC", "WIPRO", "TITAN", "MARUTI",
    "LT", "NTPC", "ONGC", "CIPLA", "SUNPHARMA", "AXISBANK", "BAJFINANCE", "HINDALCO", "NESTLEIND", "COALINDIA",
]  # fmt: skip


def entry(kind: str, summary: dict, confidence: float = 0.9) -> dict:
    return {"summary": summary, "confidence": confidence, "source": "yfinance", "evidence_id": f"ev-{kind}"}


def fundamentals(i: int) -> dict:
    return entry(
        "fund", {"pe": 18 + i, "pb": 3.1, "roe": 0.21, "de": 0.1, "net_margin": 0.19, "div_yield": 0.012}
    )


def quote(i: int) -> dict:
    return entry(
        "quote", {"price": 1000 + 17 * i, "change_pct": 0.8, "market_cap": 1.2e12, "year_high": 1300}
    )


def technicals(i: int) -> dict:
    return entry("tech", {"rsi": 40 + i, "macd": 1.2, "macd_signal": 0.9, "bb_upper": 1100, "bb_lower": 950})


def news(n: int = 8) -> dict:
    return entry("news", {"count": n}, 0.7)


def ohlcv(rows: int = 250) -> dict:
    return entry("ohlcv", {"rows": rows, "columns": ["Open", "High", "Low", "Close", "Volume"]})


cases: list[dict] = []


def add(label: str, family: str, objective: str, summary: dict) -> None:
    n = sum(1 for c in cases if c["label"] == label) + 1
    cases.append(
        {
            "id": f"{label[:3]}-{n:03d}",
            "label": label,
            "family": family,
            "objective": objective,
            "evidence_summary": summary,
        }
    )


# ── sufficient (50) ──────────────────────────────────────────────────────────
for i in range(12):
    c = COMPANIES[i]
    add("sufficient", "fundamentals", f"Analyse {c} fundamentals and valuation",
        {c: {"fundamental_ratio": fundamentals(i), "quote_info": quote(i)}})  # fmt: skip
for i in range(12, 22):
    c = COMPANIES[i - 12]
    add("sufficient", "technicals", f"Technical outlook for {c}: RSI and MACD",
        {c: {"technical_indicator": technicals(i), "price_ohlcv": ohlcv()}})  # fmt: skip
for i in range(8):
    c = COMPANIES[i + 4]
    add("sufficient", "news", f"Latest news on {c} and why it moved today",
        {c: {"news_headline": news(6 + i), "quote_info": quote(i)}})  # fmt: skip
for i in range(8):
    a, b = COMPANIES[i], COMPANIES[i + 8]
    add("sufficient", "comparison", f"Compare {a} and {b} on valuation",
        {a: {"fundamental_ratio": fundamentals(i)}, b: {"fundamental_ratio": fundamentals(i + 3)}})  # fmt: skip
for i in range(6):
    c = COMPANIES[i + 10]
    add("sufficient", "full_analysis", f"Give a full analysis of {c}",
        {c: {"fundamental_ratio": fundamentals(i), "technical_indicator": technicals(i), "news_headline": news(),
             "quote_info": quote(i)}})  # fmt: skip
for i in range(3):
    add(
        "sufficient",
        "mf",
        "Top large cap mutual funds by 1 year return",
        {"MF": {"mf_category_funds": entry("mf", {"count": 12 + i}, 0.8)}},
    )
add("sufficient", "price", "What is TCS trading at", {"TCS": {"quote_info": quote(1)}})
add("sufficient", "price", "Current price of Infosys", {"INFY": {"quote_info": quote(2)}})
add("sufficient", "price", "SBI market cap", {"SBIN": {"quote_info": quote(3)}})

# ── insufficient (50) ────────────────────────────────────────────────────────
for i in range(12):  # wrong entity
    want, have = COMPANIES[i], COMPANIES[(i + 7) % 20]
    add("insufficient", "wrong_entity", f"Analyse {want} fundamentals and valuation",
        {have: {"fundamental_ratio": fundamentals(i), "quote_info": quote(i)}})  # fmt: skip
for i in range(10):  # wrong type
    c = COMPANIES[i]
    kind = i % 3
    if kind == 0:
        add("insufficient", "wrong_type", f"Technical outlook for {c}: RSI and MACD",
            {c: {"news_headline": news()}})  # fmt: skip
    elif kind == 1:
        add("insufficient", "wrong_type", f"What is the PE and ROE of {c}",
            {c: {"technical_indicator": technicals(i)}})  # fmt: skip
    else:
        add("insufficient", "wrong_type", f"Latest news on {c}",
            {c: {"fundamental_ratio": fundamentals(i)}})  # fmt: skip
for i in range(10):  # missing comparison leg
    a, b = COMPANIES[i], COMPANIES[i + 5]
    add("insufficient", "missing_leg", f"Compare {a} and {b} on valuation",
        {a: {"fundamental_ratio": fundamentals(i)}})  # fmt: skip
for i in range(8):  # empty
    c = COMPANIES[i]
    if i % 4 == 0:
        add("insufficient", "empty", f"Analyse {c} fundamentals", {})
    elif i % 4 == 1:
        add("insufficient", "empty", f"{c} price chart for 1 year",
            {c: {"price_ohlcv": ohlcv(0)}})  # fmt: skip
    elif i % 4 == 2:
        add("insufficient", "empty", f"Latest news on {c}", {c: {"news_headline": news(0)}})
    else:
        add("insufficient", "empty", "Top flexi cap mutual funds",
            {"MF": {"mf_category_funds": entry("mf", {"count": 0}, 0.8)}})  # fmt: skip
for i in range(10):  # a bare quote answering an analysis question
    c = COMPANIES[i + 2]
    q = ["Full analysis of", "Is it a good time to look at", "Fundamental and technical view on",
         "Detailed valuation of", "Risk assessment for"][i % 5]  # fmt: skip
    add("insufficient", "low_signal", f"{q} {c}", {c: {"quote_info": quote(i)}})

if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases -> {OUT}")
