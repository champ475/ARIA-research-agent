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

from agents.equity_agent    import EquityAgent
from agents.report_agent    import ReportAgent
from agents.mf_agent        import MFAgent
from agents.screener_agent  import ScreenerAgent
from agents.alert_agent     import AlertAgent
from agents.planning_agent  import PlanningAgent
from utils.helpers          import extract_all_entities, validate_ticker
import re

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
# Multi-word phrases only — single ambiguous words like "cross" would
# misroute screener/technical queries (e.g. "stocks with RSI crossing 30").
_ALERT_KW = [
    "alert me", "notify me", "let me know if", "ping me", "stop loss",
    "stoploss", "stop-loss", "target price", "drops below", "goes above",
    "my alerts", "my alert", "cancel my alert", "cancel alert",
    "pause alert", "resume alert", "delete alert", "set an alert",
    "create an alert",
]
# Fixed phrases above miss "cancel my TCS alert" (a ticker sits between the
# verb and "alert") — catch CRUD-verb-near-"alert" with a regex instead of
# trying to enumerate every symbol position.
_ALERT_CRUD_RE = re.compile(r"\b(cancel|delete|remove|pause|resume|reactivate|unpause)\b.{0,30}\balerts?\b", re.I)

# Goal-based / multi-asset planning — deliberately phrase-based (not bare
# "portfolio") since that word alone is too generic and used to misroute
# ordinary planning questions into EquityAgent's portfolio-view command.
_PLANNING_KW = [
    "investment strategy", "financial plan", "financial planning", "asset allocation",
    "how should i invest", "how do i invest", "retirement plan", "plan for retirement",
    "diversified portfolio", "diversify my portfolio", "build my portfolio",
    "long term goal", "long-term goal", "my financial goal", "invest for my goal",
    "invest for retirement", "wealth plan", "financial profile", "my risk tolerance",
    "build my plan", "build my strategy", "investment plan",
]

_TABS = ["equity", "report", "mf", "screener", "alert", "planning"]


def route(message: str, last_tab: str | None = None) -> str:
    """Returns one of "equity"|"report"|"mf"|"screener"|"alert"|"planning" for this message."""
    q = f" {message.lower().strip()} "
    scores = {
        "report":   sum(1 for kw in _REPORT_KW   if kw in q),
        "screener": sum(1 for kw in _SCREENER_KW if kw in q),
        "mf":       sum(1 for kw in _MF_KW       if kw in q),
        "alert":    sum(1 for kw in _ALERT_KW    if kw in q) + (1 if _ALERT_CRUD_RE.search(q) else 0),
        "planning": sum(1 for kw in _PLANNING_KW if kw in q),
    }
    best_tab = max(scores, key=scores.get)
    if scores[best_tab] > 0:
        return best_tab
    # Planning is a multi-turn intake ("what's your age/capital/horizon...") —
    # follow-up answers like "I'm 30, moderate risk" won't contain any
    # planning keyword, so stay on planning rather than falling through to
    # the equity catch-all mid-conversation. But if the user clearly names a
    # stock (a valid ticker), treat that as switching topics back to equity.
    if last_tab == "planning":
        has_ticker = any(validate_ticker(t) for t in extract_all_entities(message))
        if not has_ticker:
            return "planning"
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
            "alert":    AlertAgent(user_id),
            "planning": PlanningAgent(user_id),
        }
        self._last_tab = "equity"

    def process(self, message: str) -> tuple[str, object, str | None, str | None, str | None, str]:
        """Returns (text, chart, pdf_path, md_path, csv_path, tab_used) — the final result of process_stream()."""
        result = None
        for result in self.process_stream(message):
            pass
        return result

    def process_stream(self, message: str):
        """
        Streaming counterpart to process(): yields (text, chart, pdf_path,
        md_path, csv_path, tab_used) with text growing on each yield. Only
        EquityAgent streams incrementally — report/mf/screener/alert/planning
        responses are consumed as finished documents (a saved report, a CSV
        export), not read live, so they yield their single finished result.
        """
        tab = route(message, last_tab=self._last_tab)
        self._last_tab = tab
        agent = self._agents[tab]

        if tab == "equity" and hasattr(agent, "process_stream"):
            for text, chart in agent.process_stream(message):
                yield text, chart, None, None, None, tab
            return

        if tab == "report":
            text, chart, pdf_path, md_path = agent.process(message)
            yield text, chart, pdf_path, md_path, None, tab
            return

        text, chart = agent.process(message)
        csv_path = None
        if tab == "screener":
            csv_path = agent.export_csv()
        yield text, chart, None, None, csv_path, tab

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
