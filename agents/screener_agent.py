"""
agents/screener_agent.py
Tab 4: Screener & Discovery Agent
Natural language + filter-based stock screening with top movers.
"""

import os
import csv
import re
import tempfile
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda
from agents.llm_client import LLMClient
from config import TOKENS_AGENT, TOKENS_FAST

from tools.screener          import parse_query, run as screener_run, to_markdown as screener_md
from tools.data.nse_scraper  import get_top_movers, get_bulk_deals, get_block_deals
from tools.data.fetcher      import get_macro
from tools.data.news_fetcher import market_news
from utils.helpers           import now_ist
from utils.db                import screener_save, screener_list

SYSTEM = """
You are ARIA Screener — an expert at discovering investment opportunities in Indian equity markets
using quantitative filters and qualitative analysis.

You help users:
- Screen stocks using fundamental and technical criteria
- Find top gainers, losers, and volume anomalies
- Detect bulk/block deals by FIIs, DIIs, and promoters
- Apply factor-based screening (Value, Momentum, Quality)
- Save and reuse custom screeners

SCREENING PHILOSOPHY:
- Combine multiple factors for robust screening
- Always provide context — why these filters matter
- Flag false positives (e.g., high PE may be justified for growth stocks)
- Suggest follow-up analysis for screener results

HANDLING COMPLEX / MULTI-PART REQUESTS:
- If a screen request combines multiple factor types (e.g. "quality AND value,
  low debt AND high ROE") or asks for a screen plus commentary, apply every
  filter mentioned — don't silently narrow it to just the first factor
  keyword matched — and address the extra ask (e.g. "which of these look
  safest") alongside the results table, not instead of it.
"""


class ScreenerAgent:

    def __init__(self, user_id: int):
        self._client = LLMClient(system_prompt=SYSTEM)
        self._last_results: list[dict] = []
        self._user_id = user_id

        self._nl_screen_prompt = PromptTemplate.from_template(
            'Stock screener results for: "{msg}"\n'
            "Filters applied: {filter_desc}\n"
            "Stocks found: {num_results}\n\n"
            "{table}\n\n"
            "Write:\n"
            "## Screener Results — {filter_desc_title}\n\n"
            "### Filter Analysis\n"
            "(What these filters capture and why they matter for stock selection)\n\n"
            "### Top 3 Picks from Results\n"
            "(Pick the 3 most interesting stocks with a 2-line thesis each — based on the data)\n\n"
            "### Cautions\n"
            "(Any red flags, limitations of this screen, stocks to verify further)\n\n"
            "### Follow-up Steps\n"
            "(What to check next: technical setup, quarterly results, news)\n"
        )

        self._top_movers_prompt = PromptTemplate.from_template(
            "Market movers analysis — {label}\n\n"
            "{label_upper} (from NSE):\n"
            "{mover_table}\n\n"
            "MACRO CONTEXT:\n"
            "{macro_txt}\n\n"
            "TOP HEADLINES:\n"
            "{news_txt}\n\n"
            "Write:\n"
            "## {label} — {now}\n"
            "### Market Context\n"
            "### Key Movers Analysis (which stocks and why they moved)\n"
            "### Sector Rotation Signals\n"
            "### Watch List (2-3 stocks worth monitoring after today's move)\n"
        )

        self._bulk_block_prompt = PromptTemplate.from_template(
            "Bulk and block deal analysis{sym_suffix1}.\n\n"
            "BULK DEALS:\n"
            "{bulk_txt}\n\n"
            "BLOCK DEALS:\n"
            "{block_txt}\n\n"
            "Write:\n"
            "## Bulk & Block Deal Intelligence{sym_suffix2}\n\n"
            "### What These Deals Signal\n"
            "(Interpret who is buying/selling and what it means for price action)\n\n"
            "### FII/DII Activity\n"
            "### Promoter Activity (if visible)\n"
            "### Stocks to Watch Based on Deals\n"
        )

        self._factor_screen_prompt = PromptTemplate.from_template(
            "{label} Factor Screen results.\n\n"
            "{desc}\n"
            "Combined filters (a stock must satisfy ALL of these): {filters}\n\n"
            "{table}\n\n"
            "Write:\n"
            "## {label} Factor Screen — India\n\n"
            "### Why This Factor Combination Works in Indian Markets\n"
            "### Top Picks from Screen (3 stocks with brief thesis)\n"
            "### Limitations of This Screen\n"
            "### Suggested Refinements\n"
        )

    @property
    def provider(self): return self._client.provider

    def _chat(self, prompt: str, **kwargs) -> str:
        """Wraps LLMClient.chat() so every synthesis prompt is visible in the terminal."""
        print(f"[LLM SYNTHESIS] prompt sent to LLM ({len(prompt)} chars):\n{prompt}\n[LLM SYNTHESIS] --- end prompt ---")
        return self._client.chat(prompt, **kwargs)

    def process(self, message: str) -> tuple[str, str]:
        msg   = message.strip()
        lower = msg.lower()

        # Saved screeners
        if any(k in lower for k in ["saved screener","my screeners","list screener"]):
            return self._list_screeners(), None

        # Save a screener
        if "save screener" in lower or "save this screener" in lower:
            return self._save_screener(msg), None

        # Top movers
        if any(k in lower for k in ["top gainer","top loser","biggest mover","volume spike","52 week high","52 week low","gainers","losers","top gainers","top losers"]):
            return self._top_movers(lower), None

        # Bulk/block deals
        if any(k in lower for k in ["bulk deal","block deal","fii buying","dii selling","promoter selling","insider"]):
            return self._bulk_block(msg), None

        # Factor screens — a message can combine more than one factor (e.g.
        # "quality and value screen"); check for all of them and combine
        # instead of returning on the first match and silently dropping
        # the rest.
        factors_hit = []
        if "momentum" in lower:
            factors_hit.append("momentum")
        if "value" in lower and "screen" in lower:
            factors_hit.append("value")
        if "quality" in lower and "screen" in lower:
            factors_hit.append("quality")
        if len(factors_hit) == 1:
            return self._factor_screen(factors_hit[0]), None
        if len(factors_hit) > 1:
            return self._factor_screen(factors_hit), None

        # Natural language screen
        if any(k in lower for k in ["find","screen","filter","show me stocks","stocks with","which stocks","pe below","pe above","roe above","roce above","de below","revenue growth","large cap","mid cap","small cap","midcap","largecap","smallcap","undervalued","overvalued"]):
            return self._nl_screen(msg), None

        # Fallback: try to parse as a filter screen (e.g. "IT stocks PE below 25")
        filters = parse_query.func(msg)
        if filters:
            return self._nl_screen(msg), None

        return self._help(), None

    def _nl_screen(self, msg: str) -> str:
        filters = parse_query.func(msg)
        results = screener_run.func(filters)
        self._last_results = results
        table   = screener_md.func(results, filters)

        filter_desc = ", ".join(f"{k}: {v}" for k, v in filters.items()) or "broad market"

        chain = self._nl_screen_prompt | RunnableLambda(
            lambda pv: self._chat(pv.text, temperature=0.3, max_tokens=TOKENS_AGENT)
        )
        return chain.invoke({
            "msg": msg, "filter_desc": filter_desc, "num_results": len(results),
            "table": table, "filter_desc_title": filter_desc.title(),
        })

    def _top_movers(self, lower: str) -> str:
        is_loser = "loser" in lower
        df = get_top_movers.func("losers" if is_loser else "gainers")
        macro = get_macro.func()
        news  = market_news.func()

        if df.empty:
            mover_table = "Live data unavailable — try again during market hours (9:15 AM – 3:30 PM IST)."
        else:
            cols = [c for c in ["symbol","lastPrice","pChange","totalTradedVolume"] if c in df.columns]
            # Pure string format — no tabulate dependency
            if cols:
                sub = df[cols].head(10).reset_index(drop=True)
                header = " | ".join(cols)
                sep    = " | ".join(["---"]*len(cols))
                rows   = []
                for _, row in sub.iterrows():
                    rows.append(" | ".join(str(row[c]) for c in cols))
                mover_table = "| " + header + " |\n| " + sep + " |\n" + "\n".join("| " + r + " |" for r in rows)
            else:
                mover_table = df.head(10).to_string(index=False)

        macro_txt = "\n".join(
            f"- **{k}:** {v.get('price')} ({'+' if isinstance(v.get('change'),float) and v['change']>=0 else ''}{v.get('change')}%)"
            for k,v in macro.items()
        )
        news_txt = "\n".join(f"- {n['title']}" for n in news[:5])

        label = "Top Losers" if is_loser else "Top Gainers"
        chain = self._top_movers_prompt | RunnableLambda(
            lambda pv: self._chat(pv.text, temperature=0.3, max_tokens=TOKENS_AGENT)
        )
        return chain.invoke({
            "label": label, "label_upper": label.upper(), "mover_table": mover_table,
            "macro_txt": macro_txt, "news_txt": news_txt, "now": now_ist(),
        })

    def _bulk_block(self, msg: str) -> str:
        # Try to find a symbol in message (hybrid: deterministic first, LLM
        # fallback only on ambiguity — same resolver as EquityAgent)
        from utils.helpers import resolve_query
        sym = resolve_query(msg, client=self._client)["ticker"]

        bulk  = get_bulk_deals.func(sym)
        block = get_block_deals.func(sym)

        bulk_txt  = bulk.head(10).to_string(index=False)  if not bulk.empty  else "No bulk deals found."
        block_txt = block.head(10).to_string(index=False) if not block.empty else "No block deals found."

        sym_suffix1 = f' for {sym}' if sym else ' — market-wide'
        sym_suffix2 = f' — {sym}' if sym else ''
        chain = self._bulk_block_prompt | RunnableLambda(
            lambda pv: self._chat(pv.text, temperature=0.3, max_tokens=TOKENS_AGENT)
        )
        return chain.invoke({
            "sym_suffix1": sym_suffix1, "bulk_txt": bulk_txt, "block_txt": block_txt,
            "sym_suffix2": sym_suffix2,
        })

    _FACTOR_FILTERS = {
        "value"    : {"max_pe": 20, "max_de": 1.5, "min_roce": 15},
        "momentum" : {"min_rev_growth": 15},
        "quality"  : {"min_roe": 18, "min_roce": 18, "max_de": 1.0},
    }
    _FACTOR_DESC = {
        "value"    : "Low PE, manageable debt, high ROCE — classic value screen",
        "momentum" : "High revenue growth — riding earnings momentum",
        "quality"  : "High ROE + ROCE, low leverage — quality compounders",
    }

    def _factor_screen(self, factor) -> str:
        """`factor` is either one factor name, or a list of them to combine
        into a single screen (e.g. ["quality","value"]) — a stricter,
        narrower screen than either factor alone, which is exactly what a
        "quality AND value" request is actually asking for."""
        factor_list = [factor] if isinstance(factor, str) else factor

        filters: dict = {}
        for f in factor_list:
            filters.update(self._FACTOR_FILTERS.get(f, {}))

        results = screener_run.func(filters)
        self._last_results = results
        table   = screener_md.func(results, filters)

        label = " + ".join(f.title() for f in factor_list)
        desc  = "; ".join(self._FACTOR_DESC.get(f, "") for f in factor_list if f in self._FACTOR_DESC)

        chain = self._factor_screen_prompt | RunnableLambda(
            lambda pv: self._chat(pv.text, temperature=0.3, max_tokens=TOKENS_AGENT)
        )
        return chain.invoke({
            "label": label, "desc": desc, "filters": filters, "table": table,
        })

    def _save_screener(self, msg: str) -> str:
        # Try to parse name from message
        m    = re.search(r'save\s+(?:as\s+)?["\']?([a-zA-Z0-9\s]+)["\']?', msg, re.I)
        name = m.group(1).strip() if m else "My Screener"
        filters = parse_query.func(msg)
        screener_save(self._user_id, name, filters)
        return f"✅ Screener saved as **{name}** with filters: {filters}"

    def _list_screeners(self) -> str:
        saved = screener_list(self._user_id)
        if not saved:
            return "No saved screeners yet. Run a screen and say `save screener as <name>`."
        lines = ["## Saved Screeners\n", "| Name | Filters | Created |", "|------|---------|---------|"]
        for s in saved:
            lines.append(f"| {s['name']} | {s['filters']} | {s['created_at'].strftime('%Y-%m-%d')} |")
        return "\n".join(lines)

    def _help(self) -> str:
        return (
            "## 🔍 Screener & Discovery\n\n"
            "I find stocks matching your criteria. Try:\n\n"
            "- `Screen IT stocks with PE below 25 and ROE above 15`\n"
            "- `Find undervalued midcap pharma stocks`\n"
            "- `Top gainers today`\n"
            "- `Show bulk deals`\n"
            "- `Quality factor screen`\n"
            "- `Momentum stocks`\n"
            "- `Value screen for large caps`\n"
        )

    def export_csv(self) -> str | None:
        """Write last screener results to a temp CSV and return the file path."""
        if not self._last_results:
            return None
        path = os.path.join(tempfile.gettempdir(), "aria_screener_results.csv")
        fieldnames = list(self._last_results[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self._last_results)
        return path

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        return [
            "Quality factor screen",
            "Top gainers today",
            "Bulk deals analysis",
            "Value screen large caps",
        ]

    def reset(self):
        self._client.reset()
        self._last_results = []

    def load_history(self, messages: list[dict]):
        self._client.load_history(messages)
