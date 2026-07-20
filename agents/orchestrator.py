"""
agents/orchestrator.py
Single-chat-interface orchestrator: routes each message to the right
specialist agent (equity / report / mf / screener) instead of making the
user pick a tab. Routing is deterministic keyword scoring first — matching
the rest of the codebase's "deterministic first" pattern (see
utils/helpers.py resolve_query) — with equity as the catch-all default,
since EquityAgent already handles general Q&A, portfolio, backtest, and
market-overview intents on its own.
"""

from agents.equity_agent   import EquityAgent
from agents.report_agent   import ReportAgent
from agents.mf_agent       import MFAgent
from agents.screener_agent import ScreenerAgent

# Keyword sets per specialist. Checked in this priority order — report and
# screener keywords are fairly unambiguous jargon, so they're checked before
# the broader mf category, and equity is always the fallback.
_REPORT_KW = [
    "ic report", "initiating coverage", "result update", "quarterly results",
    "sector thematic", "thematic report", "quick note", "flash report",
    "generate report", "write report", "create report", "full report",
    "list reports", "saved reports", "my reports", "show reports",
]
_SCREENER_KW = [
    "screen", "screener", "top gainer", "top loser", "biggest mover", "volume spike",
    "52 week high", "52 week low", "bulk deal", "block deal", "fii buying", "dii selling",
    "promoter selling", "insider", "momentum", "value screen", "quality screen",
    "pe below", "pe above", "roe above", "roce above", "de below", "undervalued",
    "overvalued", "stocks with", "which stocks", "save screener", "saved screener",
    "list screener", "my screeners",
]
_MF_KW = [
    "mutual fund", " sip ", "sip ", "elss", "nav of", "expense ratio",
    "flexi cap fund", "flexicap fund", "large cap fund", "mid cap fund",
    "small cap fund", "debt fund", "hybrid fund", "index fund", "lumpsum",
    "fund manager", "amc ", "direct plan", "regular plan",
]

_TABS = ["equity", "report", "mf", "screener"]


def route(message: str) -> str:
    """Returns one of "equity"|"report"|"mf"|"screener" for this message."""
    q = f" {message.lower().strip()} "
    scores = {
        "report":   sum(1 for kw in _REPORT_KW   if kw in q),
        "screener": sum(1 for kw in _SCREENER_KW if kw in q),
        "mf":       sum(1 for kw in _MF_KW       if kw in q),
    }
    best_tab = max(scores, key=scores.get)
    if scores[best_tab] > 0:
        return best_tab
    return "equity"


class Orchestrator:
    """
    Owns one instance of each specialist agent for a given user and routes
    every incoming message to the right one, normalizing their differing
    process() return shapes into a single (text, chart, pdf, md, csv, tab)
    tuple so the UI only needs to handle one response contract.
    """

    def __init__(self, user_id: int):
        self._user_id = user_id
        self._agents = {
            "equity":   EquityAgent(user_id),
            "report":   ReportAgent(user_id),
            "mf":       MFAgent(user_id),
            "screener": ScreenerAgent(user_id),
        }
        self._last_tab = "equity"

    def process(self, message: str) -> tuple[str, object, str | None, str | None, str | None, str]:
        """Returns (text, chart, pdf_path, md_path, csv_path, tab_used)."""
        tab = route(message)
        self._last_tab = tab
        agent = self._agents[tab]

        if tab == "report":
            text, chart, pdf_path, md_path = agent.process(message)
            return text, chart, pdf_path, md_path, None, tab

        text, chart = agent.process(message)
        csv_path = None
        if tab == "screener":
            csv_path = agent.export_csv()
        return text, chart, None, None, csv_path, tab

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        agent = self._agents.get(self._last_tab, self._agents["equity"])
        return agent.generate_suggestions(last_response, last_query)

    def reset(self):
        for agent in self._agents.values():
            agent.reset()
        self._last_tab = "equity"

    def load_history(self, messages: list[dict]):
        """Resumes a saved chat — seeds every specialist's memory since we
        don't know in advance which one(s) the old conversation touched."""
        for agent in self._agents.values():
            agent.load_history(messages)

    @property
    def provider(self) -> str:
        return self._agents[self._last_tab].provider
