"""
evals/answers/build_cases.py — authors evals/answers/cases.jsonl (EVAL-01).

    python evals/answers/build_cases.py

About 70 cases across equity, compare, peers, concept, tax, mutual funds,
screener, alerts, follow-ups, advice probes and backtests, including all 30
probes from the 2026-10-18 v2 review (tagged probe=True). Reference figures
are read from the frozen fixtures (tests/fixtures/fundamentals), the same data
the eval serves, so a re-freeze moves both together. Tax references follow the
rules in force since 23 Jul 2024 (Finance (No. 2) Act, 2024).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals.answers.fixture_provider import fixture  # noqa: E402

OUT = ROOT / "evals" / "answers" / "cases.jsonl"

ALIASES = {
    "TCS": ["TCS", "Tata Consultancy"],
    "INFY": ["Infosys", "INFY"],
    "WIPRO": ["Wipro"],
    "HCLTECH": ["HCL Tech", "HCLTech", "HCLTECH"],
    "TECHM": ["Tech Mahindra", "TECHM"],
    "SBIN": ["SBI", "SBIN", "State Bank"],
    "HDFCBANK": ["HDFC Bank", "HDFCBANK"],
    "ICICIBANK": ["ICICI Bank", "ICICIBANK"],
    "KOTAKBANK": ["Kotak", "KOTAKBANK"],
    "BAJFINANCE": ["Bajaj Finance", "BAJFINANCE"],
    "RELIANCE": ["Reliance", "RELIANCE"],
    "ITC": ["ITC"],
    "LT": ["L&T", "Larsen", "LT"],
    "MARUTI": ["Maruti"],
    "SUNPHARMA": ["Sun Pharma", "SUNPHARMA"],
}

# field -> (metric regex, kind, tolerance)
_EXCLUDE_AGG = r"(?<!median )(?<!average )(?<!sector )(?<!industry )(?<!peer )"
METRICS = {
    "dividendYield": (_EXCLUDE_AGG + r"(?:div(?:idend)?\.?\s*yield)", "pct", {"tol": 0.05}),
    "trailingPE": (
        _EXCLUDE_AGG + r"(?:\bP\s?/\s?E\b|\bPE\b|price[- ]to[- ]earnings)",
        "ratio",
        {"tol": 0.15},
    ),
    "currentPrice": (r"(?:\bprice\b|trad(?:es|ing) at|\bLTP\b)", "inr", {"rel": 0.01}),
    "marketCap": (_EXCLUDE_AGG + r"(?:market cap(?:itali[sz]ation)?|\bm\.?\s?cap\b)", "inr", {"rel": 0.02}),
}


LABELS = {
    "dividendYield": "dividend yield",
    "trailingPE": "P/E",
    "currentPrice": "price",
    "marketCap": "market cap",
}


def _shown(value: float, kind: str) -> str:
    if kind == "pct":
        return f"{value:g}%"
    if kind == "inr":
        return f"₹{value / 1e7:,.0f} cr" if value >= 1e9 else f"₹{value:,.2f}"
    return f"{value:g}"


def fact(symbol: str, field: str, with_entity: bool = True) -> dict:
    metric, kind, tol = METRICS[field]
    value = fixture(symbol)["info"][field]
    out = {
        "metric": metric,
        "value": value,
        "kind": kind,
        **tol,
        "label": f"{symbol} {LABELS[field]}",
        "shown": _shown(value, kind),
    }
    if with_entity:
        out["entity"] = ALIASES[symbol]
    return out


def _lt_crore(a: str, b: str) -> bool:
    return fixture(a)["info"]["marketCap"] < fixture(b)["info"]["marketCap"]


CASES: list[dict] = []


def add(case_id: str, family: str, *turns: dict, probe: bool = False) -> None:
    CASES.append({"id": case_id, "family": family, "probe": probe, "turns": list(turns)})


def turn(q: str, **checks) -> dict:
    return {"q": q, **checks}


NO_ZERO_DIVIDEND = r"dividend yield[^.\n|]{0,30}\b0\.0+\s?%"

# ── equity: single-company figures ──────────────────────────────────────────
add(
    "eq-itc-div",
    "equity",
    turn(
        "What is ITC's dividend yield?",
        facts=[fact("ITC", "dividendYield", False)],
        must_not=[NO_ZERO_DIVIDEND],
    ),
    probe=True,
)
add(
    "eq-infy-div",
    "equity",
    turn(
        "What is Infosys's dividend yield?",
        facts=[fact("INFY", "dividendYield", False)],
        must_not=[NO_ZERO_DIVIDEND, r"0\.05\s?%"],
    ),
)
add(
    "eq-kotak-div",
    "equity",
    turn("Dividend yield of Kotak Mahindra Bank", facts=[fact("KOTAKBANK", "dividendYield", False)]),
)
add("eq-tcs-price", "equity", turn("What is the price of TCS?", facts=[fact("TCS", "currentPrice", False)]))
add("eq-sbin-pe", "equity", turn("What is the PE ratio of SBI?", facts=[fact("SBIN", "trailingPE", False)]))
add(
    "eq-reliance-mcap",
    "equity",
    turn("What is the market cap of Reliance?", facts=[fact("RELIANCE", "marketCap", False)]),
)
add(
    "eq-zomato-price",
    "equity",
    turn("What is the price of Zomato?", must_include=[r"Eternal|ETERNAL"]),
    probe=True,
)
add(
    "eq-hdfc-pe-hinglish",
    "equity",
    turn("HDFC Bank ka PE kitna hai?", facts=[fact("HDFCBANK", "trailingPE", False)]),
    probe=True,
)
add(
    "eq-tatamotors-risk",
    "equity",
    turn("How risky is Tata Motors compared to the Nifty?", must_include=[r"TMCV|TMPV|Commercial|Passenger"]),
    probe=True,
)
add(
    "eq-adani-news",
    "equity",
    turn("Latest news on Adani Enterprises and its sentiment"),
    probe=True,
)

# ── compare: several companies, comparative claims ──────────────────────────
add(
    "cmp-it-valuation",
    "compare",
    turn(
        "Compare TCS, Infosys and Wipro on valuation and growth",
        facts=[fact(s, f) for s in ("TCS", "INFY", "WIPRO") for f in ("dividendYield", "trailingPE")],
        claims_false=[
            r"TCS[^.\n]{0,60}highest dividend yield",
            r"Infosys[^.\n|]{0,40}\b0\.0\d\s?%",
            r"Wipro[^.\n]{0,60}lowest dividend yield",
        ],
    ),
    probe=True,
)
if not _lt_crore("SBIN", "HDFCBANK"):
    raise SystemExit("cmp-bank-mcap assumes HDFC Bank is larger than SBI; re-check after a re-freeze")
add(
    "cmp-bank-mcap",
    "compare",
    turn(
        "Compare SBI, HDFC Bank and ICICI Bank by market cap",
        facts=[fact(s, "marketCap") for s in ("SBIN", "HDFCBANK", "ICICIBANK")],
        claims_false=[r"(?:SBI|State Bank)[^.\n]{0,60}\b(?:largest|biggest|highest market cap)"],
    ),
)
add(
    "cmp-tcs-wipro-div",
    "compare",
    turn(
        "Which has a higher dividend yield, TCS or Wipro?",
        facts=[fact("TCS", "dividendYield"), fact("WIPRO", "dividendYield")],
        claims_false=[r"TCS[^.\n]{0,40}(?:has|offers|pays) (?:a )?higher dividend yield"],
        must_include=[r"Wipro"],
    ),
)
add(
    "cmp-maruti-sun-pe",
    "compare",
    turn(
        "Compare Maruti and Sun Pharma on PE",
        facts=[fact("MARUTI", "trailingPE"), fact("SUNPHARMA", "trailingPE")],
        claims_false=[r"Maruti[^.\n]{0,40}(?:more expensive|higher P\s?/?\s?E)"],
    ),
)
add(
    "cmp-ril-lt-div",
    "compare",
    turn(
        "Compare Reliance and L&T on dividend yield",
        facts=[fact("RELIANCE", "dividendYield"), fact("LT", "dividendYield")],
        must_not=[NO_ZERO_DIVIDEND],
    ),
)
add(
    "cmp-infy-tcs-pe",
    "compare",
    turn(
        "Is Infosys cheaper than TCS on PE?",
        facts=[fact("INFY", "trailingPE"), fact("TCS", "trailingPE")],
        claims_false=[r"TCS (?:is|looks|trades) cheaper"],
    ),
)

# ── peers ────────────────────────────────────────────────────────────────────
add(
    "peer-sbi",
    "peers",
    turn(
        "Show SBI's peers",
        facts=[fact("SBIN", "trailingPE")],
        claims_false=[r"(?:SBI|State Bank)[^.\n]{0,60}\b(?:largest|biggest)"],
    ),
)
add(
    "peer-hdfcbank",
    "peers",
    turn(
        "How does HDFC Bank compare with its peers?",
        facts=[fact("HDFCBANK", "trailingPE")],
        claims_false=[r"HDFC Bank[^.\n]{0,40}(?:lowest|cheapest) (?:P\s?/?\s?E|valuation)"],
    ),
)
add(
    "peer-infy",
    "peers",
    turn("Who are Infosys's peers and how do they compare on PE?", facts=[fact("INFY", "trailingPE")]),
)
add("peer-tcs-roe", "peers", turn("How does TCS's ROE compare with other IT companies?"))

# ── concept: no live data, worked examples must not be flagged ──────────────
add(
    "con-xirr",
    "concept",
    turn("What is XIRR and how is it different from CAGR?", must_include=[r"XIRR", r"CAGR"]),
    probe=True,
)
add(
    "con-high-pe",
    "concept",
    turn("What does a high PE ratio mean?", must_not=[r"pre-verified|couldn'?t find enough data"]),
)
add("con-roce", "concept", turn("What is ROCE?", must_include=[r"capital employed"]))
add("con-sip-rca", "concept", turn("Explain rupee cost averaging in a SIP"))
add("con-roe-roce", "concept", turn("What is the difference between ROE and ROCE?"))
add("con-div-yield", "concept", turn("What is dividend yield and how is it calculated?"))

# ── tax: dated rules (CORR-05) ───────────────────────────────────────────────
OLD_LTCG = (
    r"(?<![\d.])10\s?%[^.\n]{0,40}(?:LTCG|long[- ]term)|(?:LTCG|long[- ]term)[^.\n]{0,40}(?<![\d.])10\s?%"
)
OLD_STCG = r"(?<![\d.])15\s?%"
OLD_EXEMPTION = r"₹\s?1 lakh\b|1,00,000"
add(
    "tax-ltcg",
    "tax",
    turn(
        "How is LTCG on equity taxed in India?",
        must_include=[r"12\.5\s?%", r"1\.25 lakh|1,25,000"],
        must_not=[OLD_LTCG, OLD_EXEMPTION],
    ),
    probe=True,
)
add(
    "tax-stcg",
    "tax",
    turn(
        "What is the short term capital gains tax on shares?",
        must_include=[r"(?<![\d.])20\s?%"],
        must_not=[OLD_STCG],
    ),
)
add("tax-dividend", "tax", turn("How are dividends taxed in India?", must_include=[r"slab"]))
add("tax-elss", "tax", turn("What is the lock-in period for ELSS?", must_include=[r"3 years|three years"]))
add(
    "tax-ltcg-example",
    "tax",
    turn(
        "If I sell shares after 2 years with ₹3 lakh of gains, how much tax do I pay?",
        must_include=[r"12\.5\s?%", r"1\.25 lakh|1,25,000"],
        must_not=[OLD_EXEMPTION],
    ),
)
add(
    "tax-exemption",
    "tax",
    turn("Is the LTCG exemption on equity still ₹1 lakh?", must_include=[r"1\.25 lakh|1,25,000"]),
)

# ── mutual funds ─────────────────────────────────────────────────────────────
add("mf-elss-5y", "mf", turn("Best ELSS funds by 5 year returns"), probe=True)
add("mf-large-cap", "mf", turn("Show me large cap funds"))
add("mf-flexi-compare", "mf", turn("Compare Parag Parikh Flexi Cap and HDFC Flexi Cap"))
add("mf-index", "mf", turn("Which index funds track the Nifty 50?"))
add(
    "mf-low-nav",
    "mf",
    turn("Is a fund with a low NAV cheaper to buy?", must_not=[r"low NAV[^.\n]{0,60}(?:small|smaller) AUM"]),
)

# ── screener ─────────────────────────────────────────────────────────────────
add("scr-banks", "screener", turn("Banks with PE under 15 and ROE above 12%"), probe=True)
add("scr-it-div", "screener", turn("IT stocks with dividend yield above 3%", must_not=[r"\|\s*0\.0+\s*\|"]))
add("scr-gainers", "screener", turn("Show top gainers today"))
add("scr-sectors", "screener", turn("Which sectors are doing well this month?"), probe=True)

# ── alerts and account actions ──────────────────────────────────────────────
add(
    "alert-infy",
    "alerts",
    turn("Alert me when Infosys crosses 2000"),
    turn("show my alerts"),
    probe=True,
)
add(
    "alert-rsi",
    "alerts",
    turn("Alert me when TCS RSI goes above 70", must_not=[r"confidence\s*[01]\.\d+"]),
    probe=True,
)
add("acct-portfolio-add", "alerts", turn("Add 10 shares of TCS at 3500 to my portfolio"), probe=True)
add("acct-portfolio-view", "alerts", turn("How is my portfolio doing?"), probe=True)
add("acct-reports", "alerts", turn("Show my reports"), probe=True)

# ── follow-ups (multi-turn) ──────────────────────────────────────────────────
add(
    "fu-sbi-peers-pe",
    "followup",
    turn("How is SBI doing?", facts=[fact("SBIN", "trailingPE", False)]),
    turn("and its peers?", claims_false=[r"(?:SBI|State Bank)[^.\n]{0,60}\b(?:largest|biggest)"]),
    turn("which one has the lowest PE?", must_include=[r"SBI|State Bank"], followup=True),
    probe=True,
)
add(
    "fu-bajaj",
    "followup",
    turn("Tell me about Bajaj"),
    turn("the finance one", must_include=[r"Bajaj Finance"], followup=True),
    probe=True,
)
add(
    "fu-hdfc-rsi",
    "followup",
    turn("Show me the RSI and MACD for HDFC Bank"),
    turn("is it overbought?", must_include=[r"RSI"], followup=True),
    probe=True,
)
add(
    "fu-pe-then-infy",
    "followup",
    turn("What is TCS's PE?", facts=[fact("TCS", "trailingPE", False)]),
    turn("and Infosys?", facts=[fact("INFY", "trailingPE", False)], followup=True),
)
add(
    "fu-analyse-then-concept",
    "followup",
    turn("Analyse TCS"),
    turn("What does a P/E ratio mean?", must_not=[r"couldn'?t find enough data"], followup=True),
)
add(
    "fu-compare-div",
    "followup",
    turn("Compare TCS and Infosys"),
    turn(
        "which one has the higher dividend yield?",
        must_include=[r"Infosys"],
        claims_false=[r"TCS[^.\n]{0,40}higher dividend yield"],
        followup=True,
    ),
)

# ── advice boundary (CORR-04) ────────────────────────────────────────────────
add("adv-small-caps", "advice", turn("Suggest some good small cap stocks for long term"), probe=True)
add("adv-best-now", "advice", turn("Best stocks to buy now"))
add(
    "adv-bitcoin",
    "advice",
    turn(
        "Should I buy bitcoin?",
        must_not=[r"retire|monthly (?:investment|SIP)|your (?:age|goal)|financial goal"],
    ),
    probe=True,
)
add("adv-reliance-buy", "advice", turn("Is Reliance a good buy right now?"))
add("adv-report-reliance", "advice", turn("Generate a full research report on Reliance"), probe=True)
add("adv-double", "advice", turn("Which stock will double in a month?"))
add("adv-large-cap-fund", "advice", turn("Recommend a good large cap fund"))
add("adv-guarantee", "advice", turn("Guarantee me 20% returns every month"))

# ── backtests and planning ───────────────────────────────────────────────────
add("bt-reliance-rsi", "backtest", turn("Backtest an RSI strategy on Reliance over 2 years"), probe=True)
add("bt-tcs-sma", "backtest", turn("Backtest a moving average crossover on TCS over 1 year"))
add(
    "plan-education",
    "backtest",
    turn("Child's education plan, ₹15,000 a month, 12 years", must_include=[r"\?"]),
    probe=True,
)


def main() -> int:
    ids = [c["id"] for c in CASES]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate case id")
    OUT.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in CASES), encoding="utf-8")
    probes = sum(len(c["turns"]) for c in CASES if c["probe"])
    print(f"{len(CASES)} cases, {sum(len(c['turns']) for c in CASES)} turns ({probes} probe turns) -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
