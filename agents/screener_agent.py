"""
agents/screener_agent.py
Tab 4: Screener & Discovery Agent
Natural language + filter-based stock screening with top movers.
"""

import os
import csv
import re
import tempfile
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
"""


class ScreenerAgent:

    def __init__(self, user_id: int):
        self._client = LLMClient(system_prompt=SYSTEM)
        self._last_results: list[dict] = []
        self._user_id = user_id

    @property
    def provider(self): return self._client.provider

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

        # Factor screens
        if "momentum" in lower:
            return self._factor_screen("momentum"), None
        if "value" in lower and "screen" in lower:
            return self._factor_screen("value"), None
        if "quality" in lower and "screen" in lower:
            return self._factor_screen("quality"), None

        # Natural language screen
        if any(k in lower for k in ["find","screen","filter","show me stocks","stocks with","which stocks","pe below","pe above","roe above","roce above","de below","revenue growth","large cap","mid cap","small cap","midcap","largecap","smallcap","undervalued","overvalued"]):
            return self._nl_screen(msg), None

        # Fallback: try to parse as a filter screen (e.g. "IT stocks PE below 25")
        filters = parse_query(msg)
        if filters:
            return self._nl_screen(msg), None

        return self._help(), None

    def _nl_screen(self, msg: str) -> str:
        filters = parse_query(msg)
        results = screener_run(filters)
        self._last_results = results
        table   = screener_md(results, filters)

        filter_desc = ", ".join(f"{k}: {v}" for k, v in filters.items()) or "broad market"

        prompt = f"""Stock screener results for: "{msg}"
Filters applied: {filter_desc}
Stocks found: {len(results)}

{table}

Write:
## Screener Results — {filter_desc.title()}

### Filter Analysis
(What these filters capture and why they matter for stock selection)

### Top 3 Picks from Results
(Pick the 3 most interesting stocks with a 2-line thesis each — based on the data)

### Cautions
(Any red flags, limitations of this screen, stocks to verify further)

### Follow-up Steps
(What to check next: technical setup, quarterly results, news)
"""
        return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)

    def _top_movers(self, lower: str) -> str:
        is_loser = "loser" in lower
        df = get_top_movers("losers" if is_loser else "gainers")
        macro = get_macro()
        news  = market_news()

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
        prompt = f"""Market movers analysis — {label}

{label.upper()} (from NSE):
{mover_table}

MACRO CONTEXT:
{macro_txt}

TOP HEADLINES:
{news_txt}

Write:
## {label} — {now_ist()}
### Market Context
### Key Movers Analysis (which stocks and why they moved)
### Sector Rotation Signals
### Watch List (2-3 stocks worth monitoring after today's move)
"""
        return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)

    def _bulk_block(self, msg: str) -> str:
        # Try to find a symbol in message (hybrid: deterministic first, LLM
        # fallback only on ambiguity — same resolver as EquityAgent)
        from utils.helpers import resolve_query
        sym = resolve_query(msg, client=self._client)["ticker"]

        bulk  = get_bulk_deals(sym)
        block = get_block_deals(sym)

        bulk_txt  = bulk.head(10).to_string(index=False)  if not bulk.empty  else "No bulk deals found."
        block_txt = block.head(10).to_string(index=False) if not block.empty else "No block deals found."

        prompt = f"""Bulk and block deal analysis{f' for {sym}' if sym else ' — market-wide'}.

BULK DEALS:
{bulk_txt}

BLOCK DEALS:
{block_txt}

Write:
## Bulk & Block Deal Intelligence{f' — {sym}' if sym else ''}

### What These Deals Signal
(Interpret who is buying/selling and what it means for price action)

### FII/DII Activity
### Promoter Activity (if visible)
### Stocks to Watch Based on Deals
"""
        return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)

    def _factor_screen(self, factor: str) -> str:
        FACTOR_FILTERS = {
            "value"    : {"max_pe": 20, "max_de": 1.5, "min_roce": 15},
            "momentum" : {"min_rev_growth": 15},
            "quality"  : {"min_roe": 18, "min_roce": 18, "max_de": 1.0},
        }
        filters = FACTOR_FILTERS.get(factor, {})
        results = screener_run(filters)
        self._last_results = results
        table   = screener_md(results, filters)

        FACTOR_DESC = {
            "value"    : "Low PE, manageable debt, high ROCE — classic value screen",
            "momentum" : "High revenue growth — riding earnings momentum",
            "quality"  : "High ROE + ROCE, low leverage — quality compounders",
        }

        prompt = f"""{factor.title()} Factor Screen results.

{FACTOR_DESC.get(factor, '')}
Filters: {filters}

{table}

Write:
## {factor.title()} Factor Screen — India

### Why This Factor Works in Indian Markets
### Top Picks from Screen (3 stocks with brief thesis)
### Limitations of This Screen
### Suggested Refinements
"""
        return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)

    def _save_screener(self, msg: str) -> str:
        # Try to parse name from message
        m    = re.search(r'save\s+(?:as\s+)?["\']?([a-zA-Z0-9\s]+)["\']?', msg, re.I)
        name = m.group(1).strip() if m else "My Screener"
        filters = parse_query(msg)
        screener_save(self._user_id, name, filters)
        return f"✅ Screener saved as **{name}** with filters: {filters}"

    def _list_screeners(self) -> str:
        saved = screener_list(self._user_id)
        if not saved:
            return "No saved screeners yet. Run a screen and say `save screener as <name>`."
        lines = ["## Saved Screeners\n", "| Name | Filters | Created |", "|------|---------|---------|"]
        for s in saved:
            lines.append(f"| {s['name']} | {s['filters']} | {s['created_at'][:10]} |")
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
