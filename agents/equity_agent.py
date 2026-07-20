"""
agents/equity_agent.py
Tab 1: Equity Research Agent
Full institutional-grade stock analysis with charts.
"""

import re
from concurrent.futures import ThreadPoolExecutor, wait as fut_wait, FIRST_EXCEPTION
from agents.llm_client import LLMClient
from config import TOKENS_SYNTHESIS, TOKENS_AGENT, TOKENS_FAST

from tools.data.fetcher      import get_info, get_ohlcv, get_financials, get_shareholders, get_macro, get_sector_peers, search_ticker, get_options, get_recommendations
from tools.data.nse_scraper  import get_bulk_deals, get_block_deals, get_corporate_actions, format_bulk_deals
from tools.data.pdf_scraper  import get_latest_quarterly_results, get_annual_report_text
from tools.data.news_fetcher import company_news, acquisition_news, funding_news, regulatory_news, market_news
from tools.analysis.technical   import compute_all, snapshot, support_resistance, detect_patterns, indicator_breakdown, compute_rr
from tools.analysis.fundamental import compute, to_text, calc_breakdown
from tools.analysis.sentiment   import analyse
from tools.analysis.risk        import assess
from tools.analysis.backtester  import ma_crossover_backtest, rsi_backtest, format_backtest
from tools.charts.builder       import candlestick, sector_heatmap
from tools.portfolio            import add as port_add, remove as port_remove, get_live, to_markdown as port_md
from utils.helpers              import clean_ticker, now_ist, trim_text, parse_intent, resolve_ticker

SYSTEM = """
You are ARIA Equity Research — an institutional analyst serving professional traders, portfolio managers, and analysts covering Indian equities (NSE/BSE).

RESPONSE STYLE:
- Conversational/concept → concise, 2-5 sentences
- Technical/fundamental → deep, cite every data point provided
- Full analysis → institutional-grade with explicit levels, ratios, trade setups
- Always quote exact prices, ratios, and indicator values from the data given
- Never be vague — specific numbers always beat ranges

TRADE SETUP FORMAT (always include):
- Entry zone: ₹X–X | Stop-loss: ₹X | Target 1: ₹X | Target 2: ₹X | R:R = X:1

MARKET CONTEXT: NSE/BSE, SEBI regulations, FII/DII dynamics, RBI policy, quarterly earnings cycle (Apr/Jul/Oct/Jan), F&O expiry (Thursday), Nifty/Sensex benchmarks.
"""


class EquityAgent:

    def __init__(self):
        self._client = LLMClient(system_prompt=SYSTEM)
        self._last_symbol = None

    @property
    def provider(self): return self._client.provider

    def process(self, message: str) -> tuple[str, str]:
        """Returns (markdown_text, chart_html)."""
        msg   = message.strip()
        lower = msg.lower()

        # Portfolio commands
        if self._is_port_add(lower):    return self._port_add(msg), None
        if self._is_port_remove(lower): return self._port_remove(msg), None
        if self._is_port_view(lower):   return self._port_view()

        # Backtesting
        if "backtest" in lower:
            ticker = self._ticker(msg)
            if ticker: return self._backtest(ticker, msg), None

        # Options
        if any(k in lower for k in ["option","options chain","call","put","oi","open interest"]):
            ticker = self._ticker(msg)
            if ticker: return self._options(ticker), None

        # Chart only
        if any(k in lower for k in ["chart","candlestick","show price","price chart","ohlc"]):
            ticker = self._ticker(msg)
            if ticker: return self._chart_only(ticker, msg)

        # Technical only
        if any(k in lower for k in ["technical","rsi","macd","moving average","bollinger",
                                     "support","resistance","supertrend","vwap","indicator"]):
            ticker = self._ticker(msg)
            if ticker: return self._technical(ticker, msg)

        # Fundamental only
        if any(k in lower for k in ["fundamental","valuation","pe","roe","roce","dcf","financials","earnings","balance sheet"]):
            ticker = self._ticker(msg)
            if ticker: return self._fundamental(ticker), None

        # News only
        if any(k in lower for k in ["news","latest","acquisition","merger","funding","deal","announced"]):
            ticker = self._ticker(msg)
            if ticker: return self._news(ticker), None

        # Market overview
        if any(k in lower for k in ["market overview","market today","nifty","sensex","fii","dii","macro"]):
            return self._market_overview(), None

        # Compare
        if any(k in lower for k in ["compare","vs","versus"]):
            tickers = self._multi_tickers(msg)
            if len(tickers) >= 2: return self._compare(tickers[:3]), None

        # Full analysis
        if any(k in lower for k in ["analyse","analyze","analysis","research","report","deep dive",
                                     "tell me about","should i buy","should i invest","full report"]):
            ticker = self._ticker(msg)
            if ticker: return self._full_analysis(ticker), self._get_chart(ticker)
            return self._ask_ticker(), None

        # Last resort: if the message itself contains a valid ticker, run full analysis.
        # Use resolve_ticker directly (not _ticker) to avoid matching _last_symbol
        # for purely conversational messages that contain no ticker.
        ticker, _ = resolve_ticker(msg)
        if ticker:
            self._last_symbol = ticker
            return self._full_analysis(ticker), self._get_chart(ticker)

        # No ticker in message — if a prior symbol exists, treat as follow-up
        if self._last_symbol:
            return self._technical(self._last_symbol, msg)

        # Pure conversational fallback (concepts, general questions)
        return self._client.chat(msg, max_tokens=TOKENS_AGENT), None

    # ── Ticker resolution (intent-first) ─────────────────────────────────────

    def _ticker(self, msg: str) -> str | None:
        """
        Intent-first ticker resolution.
        Never returns a stop-word as a ticker (e.g., CHART, WEEKLY, REPORT).
        Falls back to yfinance search for unknown company names.
        """
        ticker, source = resolve_ticker(msg)
        if ticker:
            self._last_symbol = ticker
            return ticker
        # Last symbol context (for follow-up queries)
        return self._last_symbol

    def _multi_tickers(self, msg: str) -> list[str]:
        from utils.helpers import STOP_WORDS
        found = re.findall(r'\b([A-Z][A-Z0-9\-&]{1,12})\b', msg.upper())
        result = []
        for t in found:
            if t not in STOP_WORDS and len(t) >= 2:
                result.append(clean_ticker(t))
        return result

    def _timeframe(self, msg: str) -> tuple[str, str, str]:
        q = msg.lower()
        if any(k in q for k in ["weekly","1w","week"]):         return "6mo","1wk","1W"
        if any(k in q for k in ["monthly","1m","month"]):       return "2y","1mo","1M"
        if any(k in q for k in ["intraday","15min","hourly"]):  return "5d","60m","1H"
        if any(k in q for k in ["6 month","6mo","6m"]):         return "6mo","1d","6M"
        if any(k in q for k in ["3 month","3mo"]):              return "3mo","1d","3M"
        if any(k in q for k in ["2 year","2y"]):                return "2y","1d","2Y"
        return "1y","1d","1D"

    # ── Full analysis ──────────────────────────────────────────────────────────

    def _full_analysis(self, sym: str) -> str:
        try:
            info  = get_info(sym)
            comp  = info.get("longName") or info.get("shortName") or sym
            if not info or (info.get("regularMarketPrice") is None and info.get("currentPrice") is None):
                return f"⚠️ Could not find data for **{sym}**. Please verify the NSE ticker symbol.\n\nTry: `analyse Reliance` or `analyse HDFCBANK`"

            # Parallel I/O: all remaining fetches are independent of each other
            with ThreadPoolExecutor(max_workers=8) as exe:
                f_ohlcv  = exe.submit(get_ohlcv, sym, "1y", "1d")
                f_fins   = exe.submit(get_financials, sym)
                f_macro  = exe.submit(get_macro)
                f_shrs   = exe.submit(get_shareholders, sym)
                f_qtext  = exe.submit(get_latest_quarterly_results, sym)
                f_news   = exe.submit(company_news, sym, comp)
                f_acq    = exe.submit(acquisition_news, comp)

            df    = f_ohlcv.result()
            fins  = f_fins.result()
            macro = f_macro.result()
            shrs  = f_shrs.result()
            q_text = f_qtext.result()
            news  = f_news.result()
            acq   = f_acq.result()

            df_ta = compute_all(df.copy()) if not df.empty else df
            ta    = snapshot(df_ta)
            sr    = support_resistance(df_ta)
            fund  = compute(info, fins)
            if ta.get("current_price"):
                fund["price"] = ta["current_price"]

            sent  = analyse(news)
            risk  = assess(fund, ta, sent["score"], macro)

            q_note = trim_text(q_text, 120) if q_text and "No " not in q_text else "Not available via scraper"

            fib  = ta.get("fib", {})
            pats = ta.get("patterns", [])
            price = fund.get("price")

            # Pre-compute R:R for trade setups
            # Short-term: use S1 as stop, R1 as target
            s1 = sr.get("S1"); r1 = sr.get("R1")
            st_rr = compute_rr(price, s1, r1, price) if all(x for x in [price, s1, r1]) else {"valid": False, "error": "Insufficient level data"}

            # Swing: use S2 as stop, R2 as target
            s2 = sr.get("S2"); r2 = sr.get("R2")
            sw_rr = compute_rr(price, s2, r2, price) if all(x for x in [price, s2, r2]) else {"valid": False, "error": "Insufficient level data"}

            # Pre-compute ALL values as plain variables — avoids f-string double-quote
            # conflicts that crash on Python 3.11 (PEP 701 only fixes this in 3.12+)
            news_titles = "\n".join(f"- {n['title']}" for n in news[:6])
            acq_titles  = "\n".join(f"- {n['title']}" for n in acq[:4])
            fund_text   = to_text(fund)
            risk_score  = risk.get("score", "N/A")
            risk_level  = risk.get("level", "N/A")
            sent_label  = sent.get("label", "N/A")
            sent_score_v= f'{sent.get("score", 0.0):+.3f}'
            risk_flags  = "; ".join(risk.get("flags", [])[:4]) or "None"

            # TA pre-computed
            ta_rsi   = ta.get("RSI");      ta_macd  = ta.get("MACD")
            ta_sig   = ta.get("MACD_Signal"); ta_hist = ta.get("MACD_Hist")
            ta_adx   = ta.get("ADX");      ta_dip   = ta.get("ADX_DI+"); ta_dim = ta.get("ADX_DI-")
            ta_atr   = ta.get("ATR");      ta_s20   = ta.get("SMA_20")
            ta_s50   = ta.get("SMA_50");   ta_s100  = ta.get("SMA_100"); ta_s200 = ta.get("SMA_200")
            ta_e9    = ta.get("EMA_9");    ta_e20   = ta.get("EMA_20");  ta_e50  = ta.get("EMA_50")
            ta_bbu   = ta.get("BB_Upper"); ta_bbm   = ta.get("BB_Mid");  ta_bbl  = ta.get("BB_Lower"); ta_bbw = ta.get("BB_Width")
            ta_sk    = ta.get("Stoch_K");  ta_sd    = ta.get("Stoch_D"); ta_vwap = ta.get("VWAP")
            ta_st    = ta.get("Supertrend"); ta_vr  = ta.get("Volume_Ratio")
            ta_stdir = "BULLISH" if (ta.get("Supertrend_Dir") or 0) > 0 else "BEARISH"
            fib_d    = fib  # already computed above
            fib_618  = fib_d.get("0.618"); fib_500 = fib_d.get("0.500"); fib_382 = fib_d.get("0.382")
            pats_str = "; ".join(pats) if pats else "No significant signals"

            # Dynamic swing levels
            swings      = sr.get("swing_highs", [])
            swing_res   = ", ".join(f"₹{v}" for v in swings) if swings else "None detected"
            swing_sup   = ", ".join(f"₹{v}" for v in sr.get("swing_lows", [])) if sr.get("swing_lows") else "None detected"

            # SR pre-computed
            sr_r2 = sr.get("R2"); sr_r1 = sr.get("R1"); sr_piv = sr.get("Pivot")
            sr_s1 = sr.get("S1"); sr_s2 = sr.get("S2")

            # R:R pre-computed
            rr_st_formula = st_rr.get("formula", "R:R calculation unavailable")
            rr_sw_formula = sw_rr.get("formula", "R:R calculation unavailable")
            rr_st_rr  = st_rr.get("rr", "N/A"); rr_sw_rr = sw_rr.get("rr", "N/A")
            rr_st_v   = "Valid" if st_rr.get("valid") else str(st_rr.get("error","Invalid setup"))
            rr_sw_v   = "Valid" if sw_rr.get("valid") else str(sw_rr.get("error","Invalid setup"))

            # Fund scorecard values
            sc_pe     = fund.get("pe");       sc_pb    = fund.get("pb")
            sc_ev     = fund.get("ev_ebitda"); sc_roe   = fund.get("roe")
            sc_roce   = fund.get("roce");      sc_npm   = fund.get("net_margin")

            # Build explainability blocks
            ind_block  = indicator_breakdown(ta)
            calc_block = calc_breakdown(fund)

            prompt = (
                f"Write a COMPLETE institutional research report for {comp} ({sym}.NS).\n"
                f"Use EVERY metric below. Do not hide any numbers.\n\n"
                f"=== FUNDAMENTAL DATA ===\n"
                f"{fund_text}\n\n"
                f"=== TECHNICAL DATA ===\n"
                f"Price {price} | RSI {ta_rsi} | MACD {ta_macd} / Signal {ta_sig} / Hist {ta_hist}\n"
                f"ADX {ta_adx} (DI+ {ta_dip} / DI- {ta_dim}) | ATR {ta_atr}\n"
                f"SMA 20={ta_s20} 50={ta_s50} 100={ta_s100} 200={ta_s200}\n"
                f"EMA 9={ta_e9} 20={ta_e20} 50={ta_e50}\n"
                f"BB Upper={ta_bbu} Mid={ta_bbm} Lower={ta_bbl} Width={ta_bbw}\n"
                f"Stoch K={ta_sk} D={ta_sd} | VWAP={ta_vwap}\n"
                f"Supertrend={ta_st} ({ta_stdir}) | Volume Ratio={ta_vr}x avg\n"
                f"S2={s2} S1={s1} Pivot={sr_piv} R1={r1} R2={r2}\n"
                f"Swing Resistances (price structure): {swing_res}\n"
                f"Swing Supports (price structure): {swing_sup}\n"
                f"Fib 61.8%={fib_618} 50%={fib_500} 38.2%={fib_382}\n"
                f"Signals: {pats_str}\n\n"
                f"=== PRE-COMPUTED TRADE SETUPS ===\n"
                f"Short-term (CMP={price} Stop=S1={s1} Target=R1={r1}):\n"
                f"{rr_st_formula}\n\n"
                f"Swing (CMP={price} Stop=S2={s2} Target=R2={r2}):\n"
                f"{rr_sw_formula}\n\n"
                f"=== RISK & SENTIMENT ===\n"
                f"Risk Score: {risk_score}/100 — {risk_level}\n"
                f"Sentiment: {sent_label} (score: {sent_score_v})\n"
                f"Risk Flags: {risk_flags}\n\n"
                f"=== QUARTERLY RESULTS ===\n"
                f"{q_note}\n\n"
                f"=== RECENT NEWS ===\n"
                f"{news_titles}\n\n"
                f"Write the report with these EXACT headers:\n\n"
                f"# {comp} ({sym}.NS) — Institutional Research Report\n\n"
                f"## Executive Summary\n"
                f"(4-5 sentences: business, thesis, catalyst, risk — every sentence must cite a number)\n\n"
                f"## Investment Scorecard\n"
                f"| Dimension | Rating | Score | Key Metrics |\n"
                f"|---|---|---|---|\n"
                f"| Valuation | X | X/10 | P/E {sc_pe}, P/B {sc_pb}, EV/EBITDA {sc_ev} |\n"
                f"| Fundamentals | X | X/10 | ROE {sc_roe}, ROCE {sc_roce}%, Net Margin {sc_npm} |\n"
                f"| Technicals | X | X/10 | RSI {ta_rsi}, ADX {ta_adx}, Supertrend {ta_stdir} |\n"
                f"| Sentiment | X | X/10 | {sent_label} score {sent_score_v} |\n"
                f"| Risk | X | X/10 | Score {risk_score}/100 |\n"
                f"| **Overall** | | **X/10** | |\n\n"
                f"## Fundamental Deep Dive\n"
                f"(All ratios, DCF scenarios, D/E calculation detail, revenue trend)\n\n"
                f"## Technical Analysis\n"
                f"**Pivot Levels:**\n"
                f"| Level | Price | Formula |\n|---|---|---|\n"
                f"| R2 | {sr_r2} | Pivot+(H-L) |\n"
                f"| R1 | {sr_r1} | 2xPivot-Low |\n"
                f"| Pivot | {sr_piv} | (H+L+C)/3 |\n"
                f"| S1 | {sr_s1} | 2xPivot-High |\n"
                f"| S2 | {sr_s2} | Pivot-(H-L) |\n\n"
                f"**Trade Setups:**\n"
                f"| Setup | Entry | Stop | Target | R:R | Status |\n|---|---|---|---|---|---|\n"
                f"| Short-term | {price} | {s1} | {r1} | {rr_st_rr}:1 | {rr_st_v} |\n"
                f"| Swing | {price} | {s2} | {r2} | {rr_sw_rr}:1 | {rr_sw_v} |\n\n"
                f"(Discuss all indicators — trend, momentum, volume, Supertrend, Fibonacci)\n\n"
                f"## News & Sentiment\n(Key events with price impact, sentiment breakdown)\n\n"
                f"## Risk Assessment\nRisk Score: {risk_score}/100 — {risk_level}\n"
                f"(Discuss each flag, probability, mitigation)\n\n"
                f"## Catalysts\n(3-4 specific events that could move stock ±10% in 6 months)\n\n"
                f"## Verdict\n"
                f"**Rating:** BUY/ACCUMULATE/HOLD/REDUCE/SELL | **Conviction:** High/Medium/Low\n"
                f"**12M Target:** X | **Stop-Loss:** {s1} | **Position:** X% of portfolio\n\n"
                f"*ARIA Equity Research | {now_ist()} | {self.provider}*\n"
                f"*Data: yfinance NSE, Google News RSS, NSE PDF scraper | Not SEBI-registered advice*"
            )
            # Get LLM analysis
            llm_output = self._client.chat(prompt, temperature=0.2, max_tokens=TOKENS_SYNTHESIS)

            # Append explainability blocks AFTER LLM output
            return llm_output + "\n\n---\n\n" + ind_block + "\n\n" + calc_block

        except Exception as e:
            import traceback
            return f"⚠️ Analysis error for **{sym}**: `{e}`\n\nPlease check the ticker symbol. Use NSE format e.g. HDFCBANK, TATAMOTORS, ATHERENERG"

    def _get_chart(self, sym: str):
        try:
            info = get_info(sym)
            comp = info.get("longName") or sym
            df   = get_ohlcv(sym, "1y", "1d")
            df_ta= compute_all(df.copy())
            return candlestick(df_ta, sym, comp, "1D")
        except Exception:
            return None

    # ── Partial analyses ───────────────────────────────────────────────────────

    def _chart_only(self, sym: str, msg: str) -> tuple[str, str]:
        try:
            info = get_info(sym)
            if not info or (not info.get("regularMarketPrice") and not info.get("currentPrice")):
                return f"⚠️ No data for **{sym}**. Check NSE ticker (e.g. ATHERENERG for Ather Energy).", None
            comp   = info.get("longName") or sym
            period, interval, tflabel = self._timeframe(msg)
            df     = get_ohlcv(sym, period, interval)
            if df is None or df.empty:
                return f"⚠️ No price history for {sym} ({period}/{interval}).", None
            df_ta  = compute_all(df.copy())
            ta     = snapshot(df_ta)
            sr     = support_resistance(df_ta)
            chart  = candlestick(df_ta, sym, comp, tflabel)

            price = ta.get("current_price")
            s1 = sr.get("S1"); r1 = sr.get("R1")
            s2 = sr.get("S2"); r2 = sr.get("R2")

            rr_short = compute_rr(price, s1, r1, price) if all(x is not None for x in [price, s1, r1]) else None
            rr_swing = compute_rr(price, s2, r2, price) if all(x is not None for x in [price, s2, r2]) else None

            def _fv(v): return f"₹{v:.2f}" if isinstance(v, float) else "N/A"
            pats = "\n".join(f"- {p}" for p in ta.get("patterns", [])[:6]) or "- No significant signals"

            rr_block = ""
            if rr_short and rr_short.get("valid"):
                rr_block += f"\n**Short-term R:R:** `{rr_short.get('formula','')}`"
            elif rr_short:
                rr_block += f"\n**Short-term R:R:** ⚠️ {rr_short.get('error','Invalid setup')}"
            if rr_swing and rr_swing.get("valid"):
                rr_block += f"\n**Swing R:R:** `{rr_swing.get('formula','')}`"

            text = (
                f"### {comp} ({sym}.NS) — {tflabel}\n"
                f"| Indicator | Value | Indicator | Value |\n"
                f"|-----------|-------|-----------|-------|\n"
                f"| CMP | {_fv(price)} | RSI (14) | {ta.get('RSI','N/A')} |\n"
                f"| SMA 50 | {_fv(ta.get('SMA_50'))} | SMA 200 | {_fv(ta.get('SMA_200'))} |\n"
                f"| MACD | {ta.get('MACD','N/A')} | Signal | {ta.get('MACD_Signal','N/A')} |\n"
                f"| ADX | {ta.get('ADX','N/A')} | ATR | {ta.get('ATR','N/A')} |\n"
                f"| VWAP | {_fv(ta.get('VWAP'))} | Vol Ratio | {ta.get('Volume_Ratio','N/A')}x |\n"
                f"| BB Upper | {_fv(ta.get('BB_Upper'))} | BB Lower | {_fv(ta.get('BB_Lower'))} |\n"
                f"\n**Pivot Levels** (formula: Pivot=(H+L+C)/3)\n"
                f"S2 ₹{s2} | S1 ₹{s1} | Pivot ₹{sr.get('Pivot')} | R1 ₹{r1} | R2 ₹{r2}\n"
                f"\n**Active Signals:**\n{pats}"
                f"{rr_block}\n"
                f"\n*Data: yfinance NSE (.NS) | Chart: Plotly candlestick with volume, RSI, MACD*"
            )
            return text, chart
        except Exception as e:
            return f"⚠️ Chart error for {sym}: `{e}`\n\nVerify ticker is listed on NSE.", None

    def _technical(self, sym: str, msg: str) -> tuple[str, str]:
        try:
            info          = get_info(sym)
            comp          = info.get("longName") or sym
            period, interval, tflabel = self._timeframe(msg)
            df            = get_ohlcv(sym, period, interval)
            df_ta         = compute_all(df.copy())
            ta            = snapshot(df_ta)
            sr            = support_resistance(df_ta)
            fib           = ta.get("fib", {})
            pats          = ta.get("patterns", [])
            chart         = candlestick(df_ta, sym, comp, tflabel)

            price = ta.get("current_price")
            s1 = sr.get("S1"); r1 = sr.get("R1")
            s2 = sr.get("S2"); r2 = sr.get("R2")
            rr_short = compute_rr(price, s1, r1, price) if all(x is not None for x in [price,s1,r1]) else {"valid":False,"error":"level data missing"}
            rr_swing = compute_rr(price, s2, r2, price) if all(x is not None for x in [price,s2,r2]) else {"valid":False,"error":"level data missing"}

            # Pre-compute all values to avoid f-string double-quote conflicts (Python 3.11)
            t_open   = ta.get("open");    t_high  = ta.get("high");   t_low   = ta.get("low")
            t_rsi    = ta.get("RSI");     t_macd  = ta.get("MACD");   t_sig   = ta.get("MACD_Signal"); t_hist = ta.get("MACD_Hist")
            t_adx    = ta.get("ADX");     t_dip   = ta.get("ADX_DI+"); t_dim  = ta.get("ADX_DI-")
            t_atr    = ta.get("ATR");     t_sk    = ta.get("Stoch_K"); t_sd   = ta.get("Stoch_D")
            t_s9     = ta.get("SMA_9");   t_s20   = ta.get("SMA_20"); t_s50  = ta.get("SMA_50"); t_s100 = ta.get("SMA_100"); t_s200 = ta.get("SMA_200")
            t_e9     = ta.get("EMA_9");   t_e20   = ta.get("EMA_20"); t_e50  = ta.get("EMA_50")
            t_bbu    = ta.get("BB_Upper"); t_bbm  = ta.get("BB_Mid"); t_bbl  = ta.get("BB_Lower"); t_bbw = ta.get("BB_Width")
            t_vwap   = ta.get("VWAP");    t_st    = ta.get("Supertrend"); t_std = ta.get("Supertrend_Dir")
            t_vr     = ta.get("Volume_Ratio"); t_vsma = ta.get("Volume_SMA20")
            t_stdir  = "BULL" if (t_std or 0) > 0 else "BEAR"
            p_pivot  = sr.get("Pivot");   p_s20   = sr.get("Support_20"); p_r20 = sr.get("Resist_20")
            sw_res   = ", ".join(f"₹{v}" for v in sr.get("swing_highs", [])) or "None detected"
            sw_sup   = ", ".join(f"₹{v}" for v in sr.get("swing_lows",  [])) or "None detected"
            fib_786  = fib.get("0.786");  fib_618 = fib.get("0.618"); fib_500 = fib.get("0.500"); fib_382 = fib.get("0.382"); fib_236 = fib.get("0.236")
            pats_str = chr(10).join(pats) if pats else "No significant signals"
            rr_short_f = rr_short.get("formula","unavailable")
            rr_swing_f = rr_swing.get("formula","unavailable")
            rr_short_rp = rr_short.get("risk_pts","?"); rr_short_rwd = rr_short.get("reward_pts","?"); rr_short_rr = rr_short.get("rr","?")
            rr_swing_rp = rr_swing.get("risk_pts","?"); rr_swing_rwd = rr_swing.get("reward_pts","?"); rr_swing_rr = rr_swing.get("rr","?")
            rr_short_v  = "✅" if rr_short.get("valid") else "❌ "+str(rr_short.get("error",""))
            rr_swing_v  = "✅" if rr_swing.get("valid") else "❌ "+str(rr_swing.get("error",""))

            prompt = f"""DEEP TECHNICAL ANALYSIS for {comp} ({sym}.NS) — {tflabel}
You MUST reference every indicator value below in your analysis.

=== ALL INDICATOR VALUES ===
CMP ₹{price} | Open ₹{t_open} | High ₹{t_high} | Low ₹{t_low}
RSI(14)={t_rsi} | MACD={t_macd} / Signal={t_sig} / Hist={t_hist}
ADX={t_adx} | DI+={t_dip} | DI-={t_dim}
ATR(14)={t_atr} | Stoch K={t_sk} / D={t_sd}
SMA 9={t_s9} | 20={t_s20} | 50={t_s50} | 100={t_s100} | 200={t_s200}
EMA 9={t_e9} | 20={t_e20} | 50={t_e50}
BB Upper={t_bbu} | Mid={t_bbm} | Lower={t_bbl} | Width={t_bbw}
VWAP={t_vwap} | Supertrend={t_st} ({t_stdir})
Volume Ratio={t_vr}x | Volume SMA20={t_vsma}

=== PIVOT LEVELS (formula: Pivot=(H+L+C)/3) ===
S2={s2} | S1={s1} | Pivot={p_pivot} | R1={r1} | R2={r2}
20D Support={p_s20} | 20D Resistance={p_r20}
Swing Resistances (structural): {sw_res}
Swing Supports (structural): {sw_sup}

=== FIBONACCI (52-week range) ===
78.6%=₹{fib_786} | 61.8%=₹{fib_618} | 50%=₹{fib_500} | 38.2%=₹{fib_382} | 23.6%=₹{fib_236}

=== ACTIVE SIGNALS ===
{pats_str}

=== PRE-COMPUTED R:R (correct formula: Reward/Risk = (Target-Entry)/(Entry-Stop)) ===
Short-term (Entry=₹{price} Stop=₹{s1} Target=₹{r1}): {rr_short_f}
Swing (Entry=₹{price} Stop=₹{s2} Target=₹{r2}): {rr_swing_f}

Write this EXACT structure:

## Technical Analysis — {comp} ({sym}.NS) | {tflabel}

### 1. Trend Analysis
(Primary and secondary trend. Price vs SMA 20/50/100/200. Supertrend direction. ADX strength interpretation.)

### 2. Momentum Analysis
(RSI: value + interpretation + any divergence. MACD: crossover status + histogram direction. Stochastic: K/D cross.)

### 3. Volatility & Volume Analysis
(BB width — squeeze or expansion? ATR as % of price. Volume ratio — is trend backed by volume?)

### 4. Key Levels Map
| Level | Price (₹) | Basis | Significance |
|-------|----------|-------|-------------|
| R2 | {r2} | Pivot formula | Strong resistance |
| R1 | {r1} | Pivot formula | First resistance |
| VWAP | {ta.get("VWAP")} | Volume-weighted | Intraday fair value |
| Pivot | {sr.get("Pivot")} | (H+L+C)/3 | Key pivot |
| CMP | {price} | Live price | — |
| S1 | {s1} | Pivot formula | First support |
| S2 | {s2} | Pivot formula | Strong support |
| Swing Resistances | {sw_res} | Price structure | Historic supply zones |
| Swing Supports | {sw_sup} | Price structure | Historic demand zones |

### 5. Trade Setups (R:R = (Target−Entry)/(Entry−Stop))
| Setup | Entry (₹) | Stop (₹) | Target (₹) | Risk (₹) | Reward (₹) | R:R | Valid? |
|-------|-----------|---------|-----------|---------|-----------|-----|--------|
| Short-term | {price} | {s1} | {r1} | {rr_short_rp} | {rr_short_rwd} | {rr_short_rr}:1 | {rr_short_v} |
| Swing | {price} | {s2} | {r2} | {rr_swing_rp} | {rr_swing_rwd} | {rr_swing_rr}:1 | {rr_swing_v} |

### 6. Overall Signal
**Technical Signal:** 🟢 STRONG BUY / 🟢 BUY / 🟡 NEUTRAL / 🔴 SELL / 🔴 STRONG SELL
**Conviction:** High / Medium / Low
**What would invalidate this setup:** [specific price level]
"""
            llm_out = self._client.chat(prompt, temperature=0.2, max_tokens=TOKENS_SYNTHESIS)
            return llm_out + "\n\n---\n\n" + indicator_breakdown(ta), chart

        except Exception as e:
            return f"⚠️ Technical error: {e}", None

    def _fundamental(self, sym: str) -> str:
        try:
            info  = get_info(sym)
            comp  = info.get("longName") or sym
            fins  = get_financials(sym)
            fund  = compute(info, fins)
            q_text= get_latest_quarterly_results(sym)
            q_note= trim_text(q_text, 120) if q_text and "No " not in q_text else ""
            prompt = f"""Deep fundamental analysis for {comp} ({sym}.NS).

{to_text(fund)}
{"Latest quarterly results summary: " + q_note if q_note else ""}

Write:
## Fundamental Analysis — {comp}
### Valuation (vs sector peers, historical avg)
### Profitability & Quality
### Growth (revenue, earnings, FCF trends)
### Balance Sheet Health
### DCF Scenarios & Analyst Consensus
### Red Flags & Positives
### Fundamental Rating: BUY/HOLD/SELL with reasoning
"""
            result = self._client.chat(prompt, temperature=0.25, max_tokens=TOKENS_AGENT)
            return result + "\n\n---\n\n" + calc_breakdown(fund)
        except Exception as e:
            return f"⚠️ Fundamental error: {e}"

    def _news(self, sym: str) -> str:
        try:
            info = get_info(sym)
            comp = info.get("longName") or sym
            news = company_news(sym, comp)
            acq  = acquisition_news(comp)
            fund_n = funding_news(comp)
            reg  = regulatory_news(comp)
            sent = analyse(news[:12])
            bulk = get_bulk_deals(sym)
            ca   = get_corporate_actions(sym)

            bulk_text  = format_bulk_deals(bulk, sym) if not bulk.empty else ""
            ca_text    = ("\n**Corporate Actions (last 5):**\n" + ca.head(5).to_string(index=False)) if not ca.empty else ""
            news_lines = "\n".join(f"- {n['title']}" for n in news[:8])
            acq_lines  = "\n".join(f"- {n['title']}" for n in acq[:4])
            fund_lines = "\n".join(f"- {n['title']}" for n in fund_n[:4])
            reg_lines  = "\n".join(f"- {n['title']}" for n in reg[:4])

            prompt = f"""News & events analysis for {comp} ({sym}.NS).

SENTIMENT: {sent['label']} (score: {sent['score']:+.3f})
{sent['text']}

COMPANY NEWS (recent):
{news_lines}

ACQUISITIONS/M&A:
{acq_lines}

FUNDING/INVESTMENT:
{fund_lines}

REGULATORY:
{reg_lines}

{bulk_text}
{ca_text}

Write:
## News & Corporate Events — {comp}
### Sentiment Overview
### Key Corporate Events (with stock impact)
### M&A & Strategic Activity
### Regulatory & Compliance
### Bulk/Block Deal Activity
### Sentiment Conclusion: POSITIVE/NEUTRAL/NEGATIVE
"""
            return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)
        except Exception as e:
            return f"⚠️ News error: {e}"

    def _options(self, sym: str) -> str:
        try:
            opt = get_options(sym)
            if not opt["available"]:
                return f"⚠️ No options data for **{sym}**."
            calls = opt["calls"][["strike","lastPrice","bid","ask","volume","openInterest","impliedVolatility"]].head(10)
            puts  = opt["puts"][["strike","lastPrice","bid","ask","volume","openInterest","impliedVolatility"]].head(10)
            prompt = f"""Options chain analysis for {sym} (Expiry: {opt['expiry']}).

CALLS (top 10 by OI):
{calls.to_string()}

PUTS (top 10 by OI):
{puts.to_string()}

Write:
## Options Chain Analysis — {sym} | Expiry {opt['expiry']}
### Max Pain & OI Levels
### PCR & Sentiment
### Implied Volatility Analysis
### Key Price Levels from Options
### Strategy Suggestions (with strikes)
### Net Signal: Bullish/Bearish/Neutral
"""
            return self._client.chat(prompt, temperature=0.25, max_tokens=TOKENS_AGENT)
        except Exception as e:
            return f"⚠️ Options error: {e}"

    def _backtest(self, sym: str, msg: str) -> str:
        try:
            df = get_ohlcv(sym, "2y", "1d")
            if "rsi" in msg.lower():
                result = rsi_backtest(df)
            else:
                fast = 20; slow = 50
                m = re.search(r'(\d+)[/,\s]+(\d+)', msg)
                if m:
                    fast, slow = int(m.group(1)), int(m.group(2))
                result = ma_crossover_backtest(df, fast=fast, slow=slow)
            return format_backtest(result)
        except Exception as e:
            return f"⚠️ Backtest error: {e}"

    def _market_overview(self) -> str:
        try:
            news  = market_news()
            macro = get_macro()
            macro_txt = "\n".join(
                f"- {'🟢' if isinstance(v.get('change'),float) and v['change'] >= 0 else '🔴'} "
                f"**{k}:** {v.get('price')} ({'+' if isinstance(v.get('change'),float) and v['change'] >= 0 else ''}{v.get('change')}%)"
                for k, v in macro.items()
            )
            news_txt = "\n".join(f"{i}. {n['title']}" for i, n in enumerate(news[:10], 1))
            prompt = f"""Indian market intelligence for traders.

MACRO:
{macro_txt}

HEADLINES:
{news_txt}

Write:
## 🇮🇳 Market Overview — {now_ist()}
### Macro Pulse (interpret moves for traders)
### Top Stories & Impact
### Sector Rotation
### Trade Ideas from News
### Risk Events This Week
"""
            return self._client.chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)
        except Exception as e:
            return f"⚠️ Market overview error: {e}"

    def _compare(self, tickers: list) -> str:
        def _fetch_one(sym):
            info  = get_info(sym)
            if not info or (not info.get("regularMarketPrice") and not info.get("currentPrice")):
                raise ValueError(f"{sym}: no price data")
            fins  = get_financials(sym)
            fund  = compute(info, fins)
            df    = get_ohlcv(sym, "6mo", "1d")
            df_ta = compute_all(df.copy()) if not df.empty else df
            ta    = snapshot(df_ta)
            return (sym, info.get("longName", sym), fund, ta)

        data   = []
        errors = []
        with ThreadPoolExecutor(max_workers=len(tickers)) as exe:
            futures = {exe.submit(_fetch_one, sym): sym for sym in tickers}
            for fut in futures:
                sym = futures[fut]
                try:
                    data.append(fut.result())
                except Exception as e:
                    errors.append(f"{sym}: {e}")

        if not data:
            err_detail = "; ".join(errors) if errors else "unknown error"
            return f"⚠️ Could not fetch data for any ticker.\n\nDetails: {err_detail}\n\nVerify tickers are valid NSE symbols."

        metrics = [("P/E","pe"),("P/B","pb"),("EV/EBITDA","ev_ebitda"),("ROE","roe"),
                   ("ROCE","roce"),("Net Margin","net_margin"),("Rev Growth","rev_growth"),
                   ("D/E","de"),("Mkt Cap","mktcap_fmt"),("Target","target_mean")]
        hdr = "| Metric | " + " | ".join(f"**{n}**" for _,n,_,_ in data) + " |"
        sep = "|--------|" + "--------|" * len(data)
        rows = [hdr, sep]
        for label, key in metrics:
            vals = [str(f.get(key,"N/A")) for _,_,f,_ in data]
            rows.append(f"| {label} | " + " | ".join(vals) + " |")
        rows.append("| RSI | " + " | ".join(str(ta.get("RSI","N/A")) for _,_,_,ta in data) + " |")
        rows.append("| vs SMA200 | " + " | ".join(
            "🟢" if (ta.get("current_price") or 0) > (ta.get("SMA_200") or 0) else "🔴"
            for _,_,_,ta in data) + " |")

        names = " vs ".join(n for _,n,_,_ in data)
        rows_str = "\n".join(rows)
        prompt = f"""Compare: {names}\n\n{rows_str}\n\nWrite:
## Comparison: {names}
### Valuation Battle | ### Profitability & Quality | ### Technical Setup | ### Risk Profile | ### Verdict
"""
        return self._client.chat(prompt, temperature=0.25, max_tokens=TOKENS_AGENT)

    # ── Portfolio ──────────────────────────────────────────────────────────────

    def _port_add(self, msg: str) -> str:
        m = re.search(r'\b([A-Z]{2,12})\b.*?(\d+(?:\.\d+)?)\s*(?:shares?|qty)?\s*(?:at|@|for|₹|rs)?\s*(\d+(?:\.\d+)?)', msg.upper())
        if m:
            return port_add(clean_ticker(m.group(1)), float(m.group(2)), float(m.group(3)))
        return "⚠️ Format: `add TCS 10 shares at 3500`"

    def _port_remove(self, msg: str) -> str:
        m = re.search(r'\b([A-Z]{2,12})\b', msg.upper())
        return port_remove(clean_ticker(m.group(1))) if m else "⚠️ Specify ticker to remove."

    def _port_view(self) -> tuple[str, str]:
        from tools.charts.builder import portfolio_chart
        holdings, summary = get_live()
        chart = portfolio_chart(holdings) if holdings else ""
        return port_md(holdings, summary), chart

    def _is_port_add(self, l):    return ("portfolio" in l or "holding" in l) and any(k in l for k in ["add","bought","buy"])
    def _is_port_remove(self, l): return ("portfolio" in l or "holding" in l) and any(k in l for k in ["remove","delete","sold"])
    def _is_port_view(self, l):   return any(k in l for k in ["my portfolio","portfolio","my holdings","show holdings"])

    def _ask_ticker(self) -> str:
        return ("Please specify a stock name or NSE ticker.\n"
                "**Examples:** `analyse TCS` · `chart RELIANCE weekly` · `compare INFY vs WIPRO`")

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        prompt = (
            f'User asked: "{last_query[:80]}". Response was about Indian stocks.\n'
            f'Return a JSON array of exactly 4 short follow-up questions (max 7 words each) '
            f'a trader would ask next. Only the JSON array, nothing else.'
        )
        try:
            import json
            raw = self._client.ask(prompt, temperature=0.8, max_tokens=TOKENS_FAST)
            raw = raw.strip().strip("```json").strip("```").strip()
            sug = json.loads(raw)
            if isinstance(sug, list): return [str(s) for s in sug[:4]]
        except Exception:
            pass
        return ["Show price chart", "Check fundamentals", "Latest news", "Backtest strategy"]

    def reset(self):
        self._client.reset()
        self._last_symbol = None
