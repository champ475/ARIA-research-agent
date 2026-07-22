"""
agents/mf_agent.py
Tab 3: Mutual Fund Analyser
Analyses MF schemes, categories, and comparisons using AMFI data.
"""

from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda
from agents.llm_client import LLMClient
from config import TOKENS_AGENT, TOKENS_FAST, TOKENS_COMPLEX
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

HANDLING COMPLEX / MULTI-PART QUESTIONS:
- Many questions bundle several asks (e.g. "split ₹50L across large/mid/small cap
  and recommend funds in each"). Identify every distinct part first, then make sure
  the answer covers all of them — don't collapse a multi-category allocation
  question into a single-category overview.
- When an amount and multiple categories are both mentioned, give a concrete
  % split across those categories with brief risk/horizon rationale, not just
  generic category education.

REGULATORY CONTEXT:
- SEBI Mutual Fund Regulations, AMFI guidelines
- Riskometer classification (Low/Moderate/High/Very High)
- SEBI fund categorization (large cap, mid cap, small cap, flexi cap, etc.)
"""


class MFAgent:

    def __init__(self, user_id: int):
        self._client = LLMClient(system_prompt=SYSTEM)
        self._nav_df = None
        self._user_id = user_id

        self._complex_advice_prompt = PromptTemplate.from_template(
            'USER QUESTION: "{msg}"\n\n'
            "This is a multi-part allocation question — silently identify every distinct part\n"
            "(e.g. how to split an amount across categories, which funds to pick in each,\n"
            "SIP vs lumpsum) and make sure your answer covers all of them. Use only the fund\n"
            "data below — never invent fund names or NAVs not present in it.\n\n"
            "=== FUND DATA ===\n{data_text}\n\n"
            "Write a direct, complete answer covering:\n"
            "- Recommended % split across the categories mentioned (with brief risk/horizon\n"
            "  rationale for the split — why more/less goes to large vs mid vs small cap)\n"
            "- 2-3 specific funds to consider per category, drawn only from the data above\n"
            "- Direct vs regular plan note\n"
            "- Key disclaimers (market risk, not SEBI-registered advice, read scheme docs)\n\n"
            "*ARIA MF Analyst | {now} | Not AMFI/SEBI registered advice*\n"
        )

        self._search_prompt = PromptTemplate.from_template(
            'User searched for mutual funds matching: "{keywords}"\n\n'
            "AMFI DATA (matching funds):\n{table}\n\n"
            "Write a helpful response:\n"
            "## Mutual Fund Search: {keywords}\n\n"
            "### Matching Funds\n{table}\n\n"
            "### Key Observations\n"
            "(Explain what these funds are, their category, and what investors should look for)\n\n"
            "### How to Choose\n"
            "(2-3 decision criteria specific to this category)\n"
        )

        self._category_prompt = PromptTemplate.from_template(
            "Analyse the {category} mutual fund category in India.\n\n"
            "TOP FUNDS (AMFI NAV data):\n{table}\n\n"
            "RECENT NEWS:\n{news_txt}\n\n"
            "Write:\n"
            "## {category_title} Mutual Funds — India\n\n"
            "### Category Overview\n"
            "(What this category invests in, SEBI classification, ideal investor)\n\n"
            "### Top Funds Analysis\n"
            "(Comment on the funds shown — NAV, what to look for)\n\n"
            "### Direct vs Regular Plan\n"
            "(Explain the cost difference and why direct is better for DIY investors)\n\n"
            "### Risk Profile\n"
            "(Riskometer level, suitable investment horizon, volatility expectations)\n\n"
            "### Who Should Invest\n"
            "(Investor profile: risk appetite, time horizon, goal)\n\n"
            "### Key Metrics to Compare\n"
            "(Expense ratio, exit load, AUM, fund manager tenure)\n\n"
            "### Our View on this Category\n"
            "(Current market context — is this a good time to invest in this category?)\n"
        )

        self._compare_prompt = PromptTemplate.from_template(
            'User wants to compare mutual funds. Message: "{msg}"\n\n'
            "{result}\n\n"
            "Write:\n"
            "## Mutual Fund Comparison\n\n"
            "### Key Differences\n"
            "### Risk-Return Analysis\n"
            "### Who Should Choose Which Fund\n"
            "### Recommendation (based on different investor profiles)\n"
        )

        self._invest_advice_prompt = PromptTemplate.from_template(
            'User investment query: "{msg}"\n\n'
            "Based on this query, provide structured mutual fund investment guidance for an Indian investor.\n\n"
            "Write:\n"
            "## MF Investment Guidance\n\n"
            "### Understanding Your Goal\n"
            "(Infer the investment goal from the query)\n\n"
            "### Recommended Category & Rationale\n"
            "### SIP vs Lumpsum — Which to Choose Now\n"
            "### Key Funds to Consider (by category, not specific picks without data)\n"
            "### Important Disclaimers\n"
            "- Past performance not indicative of future returns\n"
            "- Mutual fund investments subject to market risks\n"
            "- Read scheme documents carefully before investing\n\n"
            "*ARIA MF Analyst | {now} | Not AMFI/SEBI registered advice*\n"
        )

        self._mf_news_prompt = PromptTemplate.from_template(
            "Latest mutual fund industry news in India:\n\n"
            "{news_txt}\n\n"
            "Write a brief market intelligence note:\n"
            "## MF Industry News — {now}\n\n"
            "### Key Developments\n"
            "### Regulatory Updates\n"
            "### Impact on Investors\n"
        )

    @property
    def provider(self): return self._client.provider

    def _get_nav(self):
        if self._nav_df is None or self._nav_df.empty:
            self._nav_df = get_all_nav.func()
        return self._nav_df

    _CATEGORY_KW = ["large cap","mid cap","small cap","flexi cap","elss","liquid",
                    "hybrid","debt","index","nifty fund","sensex fund","sectoral"]

    def _is_complex(self, lower: str) -> bool:
        """
        Multi-category or multi-part MF questions (e.g. "split 50L across
        large/mid/small cap, recommend funds in each") must not collapse into
        a single-category template just because one category keyword hit
        first — that silently drops the rest of the question.
        """
        cat_hits = sum(1 for kw in self._CATEGORY_KW if kw in lower)
        if cat_hits >= 2:
            return True
        if any(w in lower for w in ["split", "allocate", "allocation", "diversify", "portfolio of"]):
            return True
        if lower.count("?") >= 2:
            return True
        conj = sum(lower.count(w) for w in [" and ", " also ", "; "])
        return conj >= 1 and len(lower) > 60

    def process(self, message: str) -> tuple[str, str]:
        msg   = message.strip()
        lower = msg.lower()

        # Multi-category / allocation questions — answer directly across every
        # category and amount mentioned, before any single-category routing
        # below gets a chance to hijack the message on its first keyword hit.
        if self._is_complex(lower):
            return self._complex_advice(msg, lower), None

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

    def _complex_advice(self, msg: str, lower: str) -> str:
        buckets = [kw for kw in self._CATEGORY_KW if kw in lower]
        if not buckets:
            # Generic "split my money" with no named categories — the standard
            # large/mid/small cap trio is the sensible default to ground the answer in.
            buckets = ["large cap", "mid cap", "small cap"]
        # Dedupe while preserving order (e.g. "large cap"/"largecap" both hitting)
        seen = set()
        buckets = [b for b in buckets if not (b in seen or seen.add(b))][:4]

        data_blocks = []
        for cat in buckets:
            funds = get_category_funds.func(cat, top_n=6)
            table = format_fund_search_results(funds)
            data_blocks.append(f"--- {cat.title()} (top funds by AMFI NAV) ---\n{table}")
        data_text = "\n\n".join(data_blocks)

        chain = self._complex_advice_prompt | RunnableLambda(
            lambda pv: self._client.chat(pv.text, temperature=0.3, max_tokens=TOKENS_COMPLEX)
        )
        return chain.invoke({"msg": msg, "data_text": data_text, "now": now_ist()})

    def _search(self, msg: str) -> str:
        # Extract fund name from query
        keywords = re.sub(r'\b(find|search|show|list|tell me about|what is|the)\b', '', msg, flags=re.I).strip()
        nav_df   = self._get_nav()
        results  = search_fund(keywords, nav_df)
        table    = format_fund_search_results(results)

        chain = self._search_prompt | RunnableLambda(
            lambda pv: self._client.chat(pv.text, temperature=0.3, max_tokens=TOKENS_AGENT)
        )
        return chain.invoke({"keywords": keywords, "table": table})

    def _category(self, category: str, msg: str) -> str:
        nav_df = self._get_nav()
        funds  = get_category_funds.func(category, top_n=12)
        table  = format_fund_search_results(funds)
        news   = google_news.func(f"India {category} mutual fund 2025", n=5)
        news_txt = "\n".join(f"- {n['title']}" for n in news[:4])

        chain = self._category_prompt | RunnableLambda(
            lambda pv: self._client.chat(pv.text, temperature=0.3, max_tokens=TOKENS_AGENT)
        )
        return chain.invoke({
            "category": category, "category_title": category.title(),
            "table": table, "news_txt": news_txt,
        })

    def _compare(self, msg: str) -> str:
        # Extract fund names to compare
        words = msg.split()
        # Simple heuristic: get capitalized words or phrases
        candidates = re.findall(r'[A-Za-z\s]+(?:fund|flexi|cap|growth|direct|regular)', msg, re.I)
        if len(candidates) < 2:
            candidates = [w for w in words if len(w) > 4 and w[0].isupper()]

        result = get_mf_comparison.func(candidates[:4]) if candidates else "Please specify 2-4 fund names to compare."

        chain = self._compare_prompt | RunnableLambda(
            lambda pv: self._client.chat(pv.text, temperature=0.3, max_tokens=TOKENS_AGENT)
        )
        return chain.invoke({"msg": msg, "result": result})

    def _invest_advice(self, msg: str) -> str:
        chain = self._invest_advice_prompt | RunnableLambda(
            lambda pv: self._client.chat(pv.text, temperature=0.3, max_tokens=TOKENS_AGENT)
        )
        return chain.invoke({"msg": msg, "now": now_ist()})

    def _nav_query(self, msg: str) -> str:
        keywords = re.sub(r'\b(nav|net asset value|price|of|the|what is|check)\b', '', msg, flags=re.I).strip()
        nav_df   = self._get_nav()
        results  = search_fund(keywords, nav_df)
        if results.empty:
            return f"Could not find NAV data for '{keywords}'. Please check the fund name."
        top = results.head(5)[["scheme_name","nav","nav_date","amc"]]
        return f"## NAV Data — {keywords}\n\n{top.to_string(index=False)}\n\n*Source: AMFI | Updated: {now_ist()}*"

    def _mf_news(self, msg: str) -> str:
        news = google_news.func(f"India mutual fund SEBI AMFI 2025", n=8)
        news_txt = "\n".join(f"- {n['title']}: {n['summary'][:120]}" for n in news[:6])
        chain = self._mf_news_prompt | RunnableLambda(
            lambda pv: self._client.chat(pv.text, temperature=0.3, max_tokens=TOKENS_AGENT)
        )
        return chain.invoke({"news_txt": news_txt, "now": now_ist()})

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        return [
            "Compare large cap funds",
            "Best ELSS funds for tax saving",
            "SIP vs lumpsum advice",
            "Flexi cap fund options",
        ]

    def reset(self): self._client.reset()

    def load_history(self, messages: list[dict]): self._client.load_history(messages)
