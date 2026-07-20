"""
agents/report_agent.py
Tab 2: Professional Report Generator
Produces Goldman Sachs / Motilal Oswal style equity research reports.
Report types: Initiating Coverage, Result Update, Sector Thematic, Quick Note
"""

import re
from agents.llm_client import LLMClient
from config import TOKENS_SYNTHESIS, TOKENS_AGENT

from tools.data.fetcher      import get_info, get_ohlcv, get_financials, get_macro, get_sector_peers, get_recommendations
from tools.data.pdf_scraper  import get_latest_quarterly_results, get_annual_report_text
from tools.data.news_fetcher import company_news, market_news
from tools.analysis.technical   import compute_all, snapshot, support_resistance
from tools.analysis.fundamental import compute, to_text
from tools.analysis.sentiment   import analyse
from tools.analysis.risk        import assess
from utils.helpers              import clean_ticker, now_ist, trim_text, parse_intent, resolve_ticker, STOP_WORDS
from utils.db                   import report_save, report_list
import os
from config import REPORTS_DIR

SYSTEM = """
You are ARIA Report Engine — you produce institutional equity research reports in the style of
Goldman Sachs, Motilal Oswal, ICICI Securities, Edelweiss, and Nomura.

REPORT STANDARDS:
- Professional, formal tone
- Lead with the investment thesis and rating
- Every claim backed by data or rationale
- Include specific price targets with methodology
- Risk section must cover company, sector, and macro risks
- Include analyst disclosures disclaimer at the end
- Use structured sections, tables, and formatting

REPORT TYPES:
1. Initiating Coverage (IC): Full deep-dive, 8-10 sections
2. Result Update (RU): Post-earnings focus, quarter analysis, guidance
3. Sector Thematic (ST): Sector-wide view, top picks, risks
4. Quick Note (QN): 1-2 page flash, rapid insight
5. Adaptive: Determine best type from user prompt
"""


class ReportAgent:

    def __init__(self, user_id: int):
        self._client      = LLMClient(system_prompt=SYSTEM)
        self._history     = []  # saved reports list
        self._last_symbol = None
        self._user_id     = user_id

    @property
    def provider(self): return self._client.provider

    def process(self, message: str) -> tuple:
        """Returns (report_markdown, chart_figure_or_none, pdf_path_or_none, md_path_or_none)."""
        msg   = message.strip()
        lower = msg.lower()

        # List saved reports
        if any(k in lower for k in ["list reports","saved reports","my reports","show reports"]):
            return self._list_reports(), None, None, None

        # Sector thematic
        if any(k in lower for k in ["sector report","thematic","sector analysis","sector outlook"]):
            text, md, pdf = self._sector_thematic(msg)
            return self._with_success(text, md), None, pdf, md

        # Quick note
        if any(k in lower for k in ["quick note","flash","brief report","quick report"]):
            ticker = self._ticker(msg)
            if not ticker:
                return "Please specify a stock symbol. Example: `Quick note on RELIANCE`", None, None, None
            text, md, pdf = self._quick_note(ticker)
            return self._with_success(text, md), None, pdf, md

        # Result update
        if any(k in lower for k in ["result update","earnings update","quarterly results","q1","q2","q3","q4","results"]):
            ticker = self._ticker(msg)
            if not ticker:
                return "Please specify a stock symbol. Example: `Result update for TCS`", None, None, None
            text, md, pdf = self._result_update(ticker, msg)
            return self._with_success(text, md), None, pdf, md

        # Initiating coverage (explicit or default for full report)
        if any(k in lower for k in ["initiating coverage","initiate","ic report","full report","generate report","write report","create report"]):
            ticker = self._ticker(msg)
            if not ticker:
                return "Please specify a stock symbol. Example: `IC report for HDFCBANK`", None, None, None
            text, md, pdf = self._initiating_coverage(ticker)
            return self._with_success(text, md), None, pdf, md

        # Any explicit stock mention → adaptive report (use resolve_ticker directly —
        # do NOT fall back to _last_symbol here so unrecognised queries show help)
        ticker, _ = resolve_ticker(msg)
        if ticker:
            self._last_symbol = ticker
            report_type = self._detect_report_type(lower)
            if report_type == "RU":
                text, md, pdf = self._result_update(ticker, msg)
                return self._with_success(text, md), None, pdf, md
            if report_type == "QN":
                text, md, pdf = self._quick_note(ticker)
                return self._with_success(text, md), None, pdf, md
            if report_type == "ST":
                text, md, pdf = self._sector_thematic(msg)
                return self._with_success(text, md), None, pdf, md
            text, md, pdf = self._initiating_coverage(ticker)
            return self._with_success(text, md), None, pdf, md

        return self._help(), None, None, None

    def _with_success(self, text: str, md_path) -> str:
        if md_path:
            return "✅ Report generated successfully. Download below.\n\n" + text
        return text

    # ── Report type detection ──────────────────────────────────────────────────

    def _detect_report_type(self, lower: str) -> str:
        if any(k in lower for k in ["earnings","results","quarterly","q1","q2","q3","q4","guidance"]):
            return "RU"
        if any(k in lower for k in ["quick","flash","brief","short","1 page"]):
            return "QN"
        if any(k in lower for k in ["sector","theme","thematic","industry"]):
            return "ST"
        return "IC"

    def _ticker(self, msg: str) -> str | None:
        """
        Intent-first ticker resolution. Updates self._last_symbol when a ticker
        is found; falls back to self._last_symbol for follow-up queries.
        e.g. 'IC report on HDFCBANK' → extracts 'HDFCBANK', not 'IC'
        """
        ticker, _ = resolve_ticker(msg)
        if ticker:
            self._last_symbol = ticker
            return ticker
        return self._last_symbol

    # ── Initiating Coverage ────────────────────────────────────────────────────

    def _initiating_coverage(self, sym: str) -> str:
        try:
            info  = get_info(sym)
            comp  = info.get("longName") or sym
            df    = get_ohlcv(sym, "2y", "1d")
            fins  = get_financials(sym)
            macro = get_macro()
            recs_df = get_recommendations(sym)

            df_ta = compute_all(df.copy()) if not df.empty else df
            ta    = snapshot(df_ta)
            sr    = support_resistance(df_ta)
            fund  = compute(info, fins)
            if ta.get("current_price"):
                fund["price"] = ta["current_price"]

            news  = company_news(sym, comp)
            sent  = analyse(news[:10])
            risk  = assess(fund, ta, sent["score"], macro)
            q_txt = get_latest_quarterly_results(sym)
            a_txt = get_annual_report_text(sym)

            q_note = trim_text(q_txt, 150) if q_txt and "No " not in q_txt else "Not available"
            a_note = trim_text(a_txt, 150) if a_txt and "No " not in a_txt else "Not available"

            dcf = fund.get("dcf") or {}
            prompt = f"""Generate a professional INITIATING COVERAGE report for {comp} ({sym}.NS) in the style of Goldman Sachs / Motilal Oswal / ICICI Securities.

COMPANY DATA:
{to_text(fund)}
RSI {ta.get('RSI')} | MACD {ta.get('MACD')} | ADX {ta.get('ADX')} | Supertrend {'BULL' if (ta.get('Supertrend_Dir') or 0) > 0 else 'BEAR'}
S1={sr.get('S1')} R1={sr.get('R1')} S2={sr.get('S2')} R2={sr.get('R2')}
Risk score: {risk['score']}/100 — {risk['level']} | Sentiment: {sent['label']}
DCF: Bear ₹{dcf.get('Bear') or (dcf.get('Bear') or dcf.get('bear', 'N/A'))} | Base ₹{dcf.get('Base') or (dcf.get('Base') or dcf.get('base', 'N/A'))} | Bull ₹{dcf.get('Bull') or (dcf.get('Bull') or dcf.get('bull', 'N/A'))}
Quarterly results: {q_note}
Annual report highlights: {a_note}

Write the FULL report with these EXACT sections (professional brokerage style):

---
**[FIRM NAME: ARIA RESEARCH]**
**{comp} ({sym}.NS)**
**INITIATING COVERAGE**
**Rating: [BUY/ACCUMULATE/HOLD/REDUCE/SELL]**
**Target Price: ₹[X] | CMP: ₹{fund.get('price')} | Upside: [X]%**
**Date: {now_ist()}**
---

## Investment Thesis
(3-5 sentences: core reason to own or avoid this stock)

## Company Overview
(Business description, revenue mix, key subsidiaries, market position)

## Industry & Competitive Landscape
(Sector dynamics, addressable market, competitive positioning, moat analysis)

## Financial Analysis
### Revenue & Profitability
(Multi-year revenue trend, margins, FCF — use data from annual report if available)

### Balance Sheet Strength
(Debt levels, working capital, capital allocation)

### Return Ratios
(ROE, ROCE, ROIC trend and peer comparison)

## Valuation
### DCF Analysis
| Scenario | FCF Growth | WACC | Intrinsic Value |
|---|---|---|---|
| Bear | 6% | 13% | ₹{dcf.get('Bear') or (dcf.get('Bear') or dcf.get('bear', 'N/A'))} |
| Base | 10% | 12% | ₹{dcf.get('Base') or (dcf.get('Base') or dcf.get('base', 'N/A'))} |
| Bull | 15% | 11% | ₹{dcf.get('Bull') or (dcf.get('Bull') or dcf.get('bull', 'N/A'))} |

### Peer Comparison & Relative Valuation
(Compare PE, PB, EV/EBITDA to 3-4 sector peers)

### Target Price Derivation
(Methodology: DCF + PE multiple / how you arrive at the TP)

## Technical Outlook
(Key levels, trend, trade setup — complement fundamental view)

## ESG & Governance
(Promoter holding, pledging, board quality, any red flags)

## Risk Factors
| Risk | Severity | Mitigation |
|---|---|---|
| (list 5 risks) | 🔴/🟠/🟡 | ... |

## Financial Summary Table
| Year | Revenue (Cr) | EBITDA (Cr) | PAT (Cr) | EPS | PE |
|---|---|---|---|---|---|
| FY23 | | | | | |
| FY24 | | | | | |
| FY25E | | | | | |
| FY26E | | | | | |

## Rating & Price Target
**Rating:** [RATING] | **Target:** ₹[TP] (12-month) | **Stop-loss:** ₹[SL]
**Previous Rating:** N/A (Initiating Coverage)

---
*ARIA Research | Analyst: ARIA AI | Date: {now_ist()}*
*Disclosures: This report is for informational purposes only. ARIA Research is not SEBI-registered.
The analyst has no personal position in {sym}. Past performance is not indicative of future results.
Investors should conduct their own due diligence before making investment decisions.*
---
"""
            report_text = self._client.chat(prompt, temperature=0.2, max_tokens=TOKENS_SYNTHESIS)
            # Save to DB
            report_save(self._user_id, sym, "Initiating Coverage", f"IC — {comp} ({sym}.NS)", report_text)
            # Save to file
            md_path, pdf_path = self._save_files(sym, "IC", report_text)
            return report_text, md_path, pdf_path

        except Exception as e:
            return f"⚠️ Report generation error: `{e}`", None, None

    # ── Result Update ─────────────────────────────────────────────────────────

    def _result_update(self, sym: str, msg: str) -> str:
        try:
            info   = get_info(sym)
            comp   = info.get("longName") or sym
            fins   = get_financials(sym)
            fund   = compute(info, fins or {})
            q_txt  = get_latest_quarterly_results(sym)
            q_note = trim_text(q_txt, 200) if q_txt and "No " not in q_txt else "PDF not available"
            news   = company_news(sym, comp)

            prompt = f"""Write a RESULT UPDATE research note for {comp} ({sym}.NS) in professional brokerage style.

FINANCIAL DATA:
{to_text(fund)}
Latest Quarterly Results (extracted): {q_note}
Recent news context: {'; '.join(n['title'] for n in news[:4])}

---
**ARIA RESEARCH — RESULT UPDATE**
**{comp} ({sym}.NS)**
**Date: {now_ist()}**
**Rating: [maintain/upgrade/downgrade to BUY/HOLD/SELL]**
**Revised Target Price: ₹[X] (Previous: ₹[X])**
---

## Quarter Highlights (Q[X] FY[X])
(Key numbers: revenue, EBITDA, PAT — vs estimates and YoY/QoQ)

## What Surprised (Positive & Negative)
## Management Commentary & Guidance
## Estimate Revision
| Metric | Old | New | Change |
|---|---|---|---|
| Revenue FY25E | | | |
| EBITDA FY25E | | | |
| EPS FY25E | | | |

## Valuation Impact
## Our View & Rating Action
*ARIA Research | {now_ist()} | Not SEBI-registered advice*
"""
            report_text = self._client.chat(prompt, temperature=0.2, max_tokens=TOKENS_AGENT)
            report_save(self._user_id, sym, "Result Update", f"RU — {comp} Q-Results", report_text)
            md_path, pdf_path = self._save_files(sym, "RU", report_text)
            return report_text, md_path, pdf_path

        except Exception as e:
            return f"⚠️ Result update error: `{e}`", None, None

    # ── Sector Thematic ────────────────────────────────────────────────────────

    def _sector_thematic(self, msg: str) -> str:
        SECTOR_MAP = {
            "it": "Technology", "tech": "Technology", "bank": "Financial Services",
            "pharma": "Healthcare", "auto": "Consumer Cyclical", "energy": "Energy",
            "metal": "Metals & Mining", "fmcg": "Consumer Defensive",
            "infra": "Industrials", "realty": "Real Estate",
        }
        sector = "Technology"
        for kw, s in SECTOR_MAP.items():
            if kw in msg.lower():
                sector = s; break

        from config import SECTOR_UNIVERSE
        peers = SECTOR_UNIVERSE.get(sector, [])[:6]
        peer_data = []
        for p in peers:
            try:
                info = get_info(p)
                fins = get_financials(p)
                fund = compute(info, fins)
                peer_data.append((p, info.get("shortName", p), fund))
            except Exception:
                continue

        macro  = get_macro()
        m_news = market_news()

        peer_table = "| Stock | PE | ROE% | Rev Gr% | Mkt Cap |\n|---|---|---|---|---|\n" + "\n".join(
            f"| **{sym}** | {f.get('pe','N/A')} | {f.get('roe','N/A')} | {f.get('rev_growth','N/A')} | {f.get('mktcap_fmt','N/A')} |"
            for sym, _, f in peer_data
        )

        prompt = f"""Write a SECTOR THEMATIC REPORT for the {sector} sector in India (professional brokerage style).

SECTOR STOCKS DATA:
{peer_table}

MACRO CONTEXT:
{'; '.join(f"{k}: {v.get('price')} ({'+' if isinstance(v.get('change'),float) and v['change']>=0 else ''}{v.get('change')}%)" for k,v in macro.items())}

RECENT HEADLINES:
{'; '.join(n['title'] for n in m_news[:6])}

---
**ARIA RESEARCH — SECTOR THEMATIC**
**Indian {sector} Sector**
**Date: {now_ist()}**
---

## Sector Overview & Investment Thesis
## Macro Tailwinds & Headwinds
## Regulatory & Policy Environment
## Peer Comparison Table (use the data above)
## Top Picks
| Stock | Rating | CMP | Target | Upside | Thesis |
|---|---|---|---|---|---|
| (top 2-3 stocks with reasoning) | | | | | |
## Risks to Sector View
## Conclusion
*ARIA Research | {now_ist()} | Not SEBI-registered advice*
"""
        report_text = self._client.chat(prompt, temperature=0.25, max_tokens=TOKENS_AGENT)
        report_save(self._user_id, "", "Sector Thematic", f"Thematic — {sector} Sector", report_text)
        try:
            md_path, pdf_path = self._save_files("SECTOR", "ST", report_text)
        except Exception:
            md_path = pdf_path = None
        return report_text, md_path, pdf_path

    # ── Quick Note ─────────────────────────────────────────────────────────────

    def _quick_note(self, sym: str) -> str:
        try:
            info = get_info(sym)
            comp = info.get("longName") or sym
            fins = get_financials(sym)
            fund = compute(info, fins)
            news = company_news(sym, comp)

            prompt = f"""Write a QUICK NOTE / FLASH NOTE for {comp} ({sym}.NS).
Keep it to 1 page. Professional. Punchy. Actionable.

DATA: {to_text(fund)}
Recent news: {'; '.join(n['title'] for n in news[:3])}

---
**ARIA QUICK NOTE | {comp} ({sym}.NS) | {now_ist()}**
**Rating: [BUY/HOLD/SELL] | CMP: ₹{fund.get('price')} | Target: ₹[X]**

**Key Takeaway** (2 sentences max)

**Why Now** (3 bullet points)

**Key Risk** (1 bullet)

**Trade Setup:** Entry ₹? | Stop ₹? | Target ₹? | R:R ?:1

*ARIA Research | Not SEBI-registered advice*
"""
            report_text = self._client.chat(prompt, temperature=0.3, max_tokens=600)
            report_save(self._user_id, sym, "Quick Note", f"QN — {comp}", report_text)
            md_path, pdf_path = self._save_files(sym, "QN", report_text)
            return report_text, md_path, pdf_path
        except Exception as e:
            return f"⚠️ Quick note error: `{e}`", None, None

    # ── Report management ──────────────────────────────────────────────────────

    def _list_reports(self) -> str:
        reports = report_list(self._user_id, 20)
        if not reports:
            return "No reports saved yet. Generate one with: `IC report for TCS`"
        lines = ["## Saved Reports\n", "| # | Type | Title | Date |", "|---|------|-------|------|"]
        for i, r in enumerate(reports, 1):
            lines.append(f"| {i} | {r['report_type']} | {r['title'][:40]} | {r['created_at'][:10]} |")
        return "\n".join(lines)

    def _save_files(self, sym: str, rtype: str, content: str) -> tuple:
        """Save report as Markdown and (optionally) PDF. Returns (md_path, pdf_path)."""
        os.makedirs(REPORTS_DIR, exist_ok=True)
        ts   = now_ist().replace(" ", "_").replace(",", "").replace(":", "")
        base = os.path.join(REPORTS_DIR, f"{sym}_{rtype}_{ts[:15]}")
        md_path = pdf_path = None

        try:
            md_path = base + ".md"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            md_path = None

        try:
            import markdown as _md
            from weasyprint import HTML as _HTML
            html_body = _md.markdown(content, extensions=["tables", "fenced_code"])
            html_full = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
                "body{font-family:Arial,sans-serif;font-size:12px;margin:40px;color:#222}"
                "h1,h2,h3{color:#1a1a2e}"
                "table{border-collapse:collapse;width:100%;margin:12px 0}"
                "th,td{border:1px solid #ccc;padding:6px 10px;text-align:left}"
                "th{background:#f0f4f8;font-weight:bold}"
                "code{background:#f5f5f5;padding:2px 4px;border-radius:3px;font-size:11px}"
                f"</style></head><body>{html_body}</body></html>"
            )
            pdf_path = base + ".pdf"
            _HTML(string=html_full).write_pdf(pdf_path)
        except Exception:
            pdf_path = None

        return md_path, pdf_path

    def _help(self) -> str:
        return (
            "## 📋 Report Generator\n\n"
            "I generate professional equity research reports. Try:\n\n"
            "- `Initiating Coverage on TCS` — full IC report\n"
            "- `Result update for Reliance` — post-earnings note\n"
            "- `IT sector thematic report` — sector-wide view\n"
            "- `Quick note on INFY` — 1-page flash note\n"
            "- `List reports` — see all saved reports\n"
        )

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        return [
            "Generate IC report",
            "Result update note",
            "IT sector thematic",
            "Quick note on HDFC",
        ]

    def reset(self):
        self._client.reset()
        self._last_symbol = None
