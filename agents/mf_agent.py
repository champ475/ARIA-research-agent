"""
agents/mf_agent.py
Tab 3: Mutual Fund Analyser
Analyses MF schemes, categories, and comparisons using AMFI data.
"""

from agents.llm_client import LLMClient
from config import TOKENS_AGENT, TOKENS_FAST
from tools.data.amfi import get_all_nav, search_fund, get_category_funds, get_mf_comparison, format_fund_search_results
from tools.data.news_fetcher import google_news
from utils.helpers import now_ist
import re

SYSTEM = """
You are ARIA Mutual Fund Analyst — an expert in Indian mutual funds, SEBI regulations,
SIP investing, and fund analysis for retail and institutional investors.

You help users:
- Compare and select mutual funds across categories
- Understand fund performance, risk metrics, and expense ratios
- Explain fund strategies (large-cap, flexi-cap, ELSS, debt, hybrid)
- Give SIP/lumpsum recommendations based on investment goals
- Explain AMC-specific strategies and fund manager track records

ALWAYS:
- Quote current NAV from AMFI data when available
- Explain risk-adjusted returns (not just absolute returns)
- Mention expense ratio impact on long-term returns
- Clarify direct vs regular plan differences (expense ratio ~0.5-1.5% difference)
- Never recommend specific funds without data — always ground in facts

REGULATORY CONTEXT:
- SEBI Mutual Fund Regulations, AMFI guidelines
- Riskometer classification (Low/Moderate/High/Very High)
- SEBI fund categorization (large cap, mid cap, small cap, flexi cap, etc.)
"""


class MFAgent:

    def __init__(self):
        self._client = LLMClient(system_prompt=SYSTEM)
        self._nav_df = None

    @property
    def provider(self): return self._client.provider

    def _get_nav(self):
        if self._nav_df is None or self._nav_df.empty:
            self._nav_df = get_all_nav()
        return self._nav_df

    def process(self, message: str) -> tuple[str, str]:
        msg   = message.strip()
        lower = msg.lower()

        # Compare funds — if category keywords present, route to category instead
        if any(k in lower for k in ["compare","vs","versus"]):
            for kw in ["large cap","largecap","mid cap","midcap","small cap","smallcap",
                       "flexi cap","flexicap","elss","debt","liquid","hybrid","index","sectoral"]:
                if kw in lower:
                    return self._category(kw, msg), None
            return self._compare(msg), None

        # Category search
        for kw in ["large cap","mid cap","small cap","flexi cap","elss","liquid","hybrid",
                   "debt","index","nifty fund","sensex fund","sectoral"]:
            if kw in lower:
                return self._category(kw, msg), None

        # Search specific fund
        if any(k in lower for k in ["find","search","show","list","what is","tell me about"]):
            return self._search(msg), None

        # SIP advice
        if any(k in lower for k in ["sip","lumpsum","invest","recommend","best fund","which fund"]):
            return self._invest_advice(msg), None

        # News about AMCs or MF industry
        if any(k in lower for k in ["news","amfi","sebi","amc","fund house"]):
            return self._mf_news(msg), None

        # NAV query
        if any(k in lower for k in ["nav","net asset value","price"]):
            return self._nav_query(msg), None

        # General MF question
        return self._client.chat(msg, max_tokens=TOKENS_AGENT), None

    def _search(self, msg: str) -> str:
        # Extract fund name from query
        keywords = re.sub(r'\b(find|search|show|list|tell me about|what is|the)\b', '', msg, flags=re.I).strip()
        nav_df   = self._get_nav()
        results  = search_fund(keywords, nav_df)
        table    = format_fund_search_results(results)

        prompt = f"""User searched for mutual funds matching: "{keywords}"

AMFI DATA (matching funds):
{table}

Write a helpful response:
## Mutual Fund Search: {keywords}

### Matching Funds
{table}

### Key Observations
(Explain what these funds are, their category, and what investors should look for)

### How to Choose
(2-3 decision criteria specific to this category)
"""
        return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)

    def _category(self, category: str, msg: str) -> str:
        nav_df = self._get_nav()
        funds  = get_category_funds(category, top_n=12)
        table  = format_fund_search_results(funds)
        news   = google_news(f"India {category} mutual fund 2025", n=5)
        news_txt = "\n".join(f"- {n['title']}" for n in news[:4])

        prompt = f"""Analyse the {category} mutual fund category in India.

TOP FUNDS (AMFI NAV data):
{table}

RECENT NEWS:
{news_txt}

Write:
## {category.title()} Mutual Funds — India

### Category Overview
(What this category invests in, SEBI classification, ideal investor)

### Top Funds Analysis
(Comment on the funds shown — NAV, what to look for)

### Direct vs Regular Plan
(Explain the cost difference and why direct is better for DIY investors)

### Risk Profile
(Riskometer level, suitable investment horizon, volatility expectations)

### Who Should Invest
(Investor profile: risk appetite, time horizon, goal)

### Key Metrics to Compare
(Expense ratio, exit load, AUM, fund manager tenure)

### Our View on this Category
(Current market context — is this a good time to invest in this category?)
"""
        return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)

    def _compare(self, msg: str) -> str:
        # Extract fund names to compare
        words = msg.split()
        # Simple heuristic: get capitalized words or phrases
        candidates = re.findall(r'[A-Za-z\s]+(?:fund|flexi|cap|growth|direct|regular)', msg, re.I)
        if len(candidates) < 2:
            candidates = [w for w in words if len(w) > 4 and w[0].isupper()]

        result = get_mf_comparison(candidates[:4]) if candidates else "Please specify 2-4 fund names to compare."

        prompt = f"""User wants to compare mutual funds. Message: "{msg}"

{result}

Write:
## Mutual Fund Comparison

### Key Differences
### Risk-Return Analysis
### Who Should Choose Which Fund
### Recommendation (based on different investor profiles)
"""
        return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)

    def _invest_advice(self, msg: str) -> str:
        prompt = f"""User investment query: "{msg}"

Based on this query, provide structured mutual fund investment guidance for an Indian investor.

Write:
## MF Investment Guidance

### Understanding Your Goal
(Infer the investment goal from the query)

### Recommended Category & Rationale
### SIP vs Lumpsum — Which to Choose Now
### Key Funds to Consider (by category, not specific picks without data)
### Important Disclaimers
- Past performance not indicative of future returns
- Mutual fund investments subject to market risks
- Read scheme documents carefully before investing

*ARIA MF Analyst | {now_ist()} | Not AMFI/SEBI registered advice*
"""
        return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)

    def _nav_query(self, msg: str) -> str:
        keywords = re.sub(r'\b(nav|net asset value|price|of|the|what is|check)\b', '', msg, flags=re.I).strip()
        nav_df   = self._get_nav()
        results  = search_fund(keywords, nav_df)
        if results.empty:
            return f"Could not find NAV data for '{keywords}'. Please check the fund name."
        top = results.head(5)[["scheme_name","nav","nav_date","amc"]]
        return f"## NAV Data — {keywords}\n\n{top.to_string(index=False)}\n\n*Source: AMFI | Updated: {now_ist()}*"

    def _mf_news(self, msg: str) -> str:
        news = google_news(f"India mutual fund SEBI AMFI 2025", n=8)
        news_txt = "\n".join(f"- {n['title']}: {n['summary'][:120]}" for n in news[:6])
        prompt = f"""Latest mutual fund industry news in India:

{news_txt}

Write a brief market intelligence note:
## MF Industry News — {now_ist()}

### Key Developments
### Regulatory Updates
### Impact on Investors
"""
        return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        return [
            "Compare large cap funds",
            "Best ELSS funds for tax saving",
            "SIP vs lumpsum advice",
            "Flexi cap fund options",
        ]

    def reset(self): self._client.reset()
