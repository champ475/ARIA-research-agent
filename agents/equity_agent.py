"""
agents/equity_agent.py
Tab 1: Equity Research Agent
Adaptive stock Q&A — always fetches full data (fundamentals, 1Y short-term
levels, 2Y long-term levels, risk, sentiment, news), always feeds the user's
verbatim question to the LLM, and lets the LLM decide response shape instead
of routing through rigid per-intent report templates.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from agents.llm_client import LLMClient
from config import TOKENS_SYNTHESIS, TOKENS_COMPLEX, TOKENS_AGENT, TOKENS_FAST

from tools.data.fetcher      import get_info, get_ohlcv, get_financials, get_macro, get_options
from tools.data.pdf_scraper  import get_latest_quarterly_results
from tools.data.news_fetcher import company_news, market_news
from tools.analysis.technical   import compute_all, snapshot, support_resistance, indicator_breakdown, compute_rr
from tools.analysis.fundamental import compute, to_text, calc_breakdown
from tools.analysis.sentiment   import analyse
from tools.analysis.risk        import assess
from tools.analysis.backtester  import ma_crossover_backtest, rsi_backtest, format_backtest
from tools.charts.builder       import candlestick
from tools.portfolio            import add as port_add, remove as port_remove, get_live, to_markdown as port_md
from utils.helpers              import clean_ticker, now_ist, trim_text, resolve_query, extract_all_entities, validate_ticker

SYSTEM = """
You are ARIA Equity Research — an institutional-grade financial analyst for Indian equities (NSE/BSE), reasoning and communicating at the level of a senior analyst chatting with a professional trader. Match the rigor, structure, and clarity of a top-tier AI research assistant: think before you answer, never pad with filler, never dodge a hard question.

RESPONSE STYLE:
- Answer the user's actual question directly and specifically first — never bury it under a generic template.
- Cite exact numbers, prices, ratios, and indicator values from the data given — never be vague, never invent figures. If the data needed to answer isn't present in the bundle below, say so explicitly instead of guessing.
- Only include supporting sections beyond the direct answer when they're relevant to what was actually asked.
- For broad/generic requests ("analyse X", "tell me about X"), fall back to the full institutional report structure.

HANDLING COMPLEX / MULTI-PART QUESTIONS:
- Many questions bundle several distinct asks in one message (e.g. "what's the trend, is it a buy, and how does it compare to its sector peers on valuation"). Silently identify every distinct sub-question first, then make sure your answer addresses each one — use short headers or bullets per sub-question when there are 3+ of them so nothing gets skipped.
- Reason step by step internally (trend → valuation → risk → verdict) before writing the final answer, but only output the conclusions and the numbers that support them — never show raw chain-of-thought.
- If a question is genuinely ambiguous (no ticker, no clear ask, could mean several different things), ask ONE precise clarifying question instead of guessing and answering the wrong thing.
- Where reasonable analysts could disagree (e.g. growth-vs-value framing, aggressive vs conservative stop-loss), say so and give your reasoned take — don't hide behind false balance.

TRADE SETUP FORMAT (when relevant):
- Entry zone: ₹X–X | Stop-loss: ₹X | Target: ₹X | R:R = X:1
- Always distinguish SHORT-TERM (1Y-based) levels from LONG-TERM (2Y-based) levels — they are different numbers, never conflate them.

MARKET CONTEXT: NSE/BSE, SEBI regulations, FII/DII dynamics, RBI policy, quarterly earnings cycle (Apr/Jul/Oct/Jan), F&O expiry (Thursday), Nifty/Sensex benchmarks.

This is analysis, not investment advice — end responses with a brief reminder of that when giving a directional call (buy/sell/hold, entry/stop/target).
"""


class EquityAgent:

    def __init__(self, user_id: int):
        self._client = LLMClient(system_prompt=SYSTEM)
        self._last_symbol = None
        self._user_id = user_id

    @property
    def provider(self): return self._client.provider

    def _chat(self, prompt: str, **kwargs) -> str:
        """Wraps LLMClient.chat() so every synthesis prompt is visible in the terminal."""
        print(f"[LLM SYNTHESIS] prompt sent to LLM ({len(prompt)} chars):\n{prompt}\n[LLM SYNTHESIS] --- end prompt ---")
        return self._client.chat(prompt, **kwargs)

    def _ask(self, prompt: str, **kwargs) -> str:
        """Wraps LLMClient.ask() so every stateless prompt is visible in the terminal."""
        print(f"[LLM SYNTHESIS] prompt sent to LLM ({len(prompt)} chars):\n{prompt}\n[LLM SYNTHESIS] --- end prompt ---")
        return self._client.ask(prompt, **kwargs)

    def process(self, message: str) -> tuple[str, str]:
        """Returns (markdown_text, chart_html)."""
        msg   = message.strip()
        lower = msg.lower()

        # Portfolio commands
        if self._is_port_add(lower):    return self._port_add(msg), None
        if self._is_port_remove(lower): return self._port_remove(msg), None
        if self._is_port_view(lower):   return self._port_view()

        # Backtesting — equity-curve data, a genuinely different shape from a
        # stock Q&A answer, kept as its own path.
        if "backtest" in lower:
            ticker = self._ticker(msg)
            if ticker: return self._backtest(ticker, msg), None
            return self._ask_ticker(), None

        # Market overview — macro feed, no ticker involved, kept separate.
        if any(k in lower for k in ["market overview","market today","nifty","sensex","fii","dii","macro"]):
            return self._market_overview(), None

        # Everything else: resolve ticker(s) for the message, then let the LLM
        # answer the actual question using the full data bundle. See
        # utils/helpers.py resolve_query() / extract_all_entities() for the
        # resolution logic (deterministic-first, LLM fallback on ambiguity).
        entities = extract_all_entities(msg)
        valid    = [t for t in entities if validate_ticker(t)]
        print(f"[PARSE] entities found in message: {entities} | valid: {valid}")

        det = resolve_query(msg, client=self._client, last_symbol=self._last_symbol)

        if len(valid) >= 2:
            # Explicit multi-ticker mention in one message (e.g. "TCS or HDFC Bank")
            tickers = valid[:3]
        elif det["intent"] == "compare" and det["ticker"] and self._last_symbol and det["ticker"] != self._last_symbol:
            # e.g. "analyse TCS" then "compare with HDFC bank" — pull in the
            # previously discussed stock instead of silently dropping it.
            tickers = [self._last_symbol, det["ticker"]]
        elif det["ticker"]:
            tickers = [det["ticker"]]
        elif self._last_symbol:
            # No ticker in this message — treat as a follow-up on the last stock.
            tickers = [self._last_symbol]
        else:
            # Nothing to anchor on — plain conversational fallback (market concepts,
            # general finance questions). Still give complex asks room to breathe.
            budget = TOKENS_COMPLEX if self._is_complex(msg, False) else TOKENS_AGENT
            return self._chat(msg, max_tokens=budget), None

        self._last_symbol = tickers[-1]
        return self._answer(tickers, msg)

    # ── Ticker resolution ──────────────────────────────────────────────────────

    def _ticker(self, msg: str) -> str | None:
        """
        Hybrid ticker resolution: deterministic first, LLM fallback only on
        ambiguity (see utils.helpers.resolve_query for exact trigger conditions).
        Never returns an unvalidated ticker. Falls back to the last resolved
        symbol for follow-up queries with no ticker of their own.
        """
        result = resolve_query(msg, client=self._client, last_symbol=self._last_symbol)
        if result["ticker"]:
            self._last_symbol = result["ticker"]
            return result["ticker"]
        return self._last_symbol

    def _timeframe(self, msg: str) -> tuple[str, str, str]:
        q = msg.lower()
        if any(k in q for k in ["weekly","1w","week"]):         return "6mo","1wk","1W"
        if any(k in q for k in ["monthly","1m","month"]):       return "2y","1mo","1M"
        if any(k in q for k in ["intraday","15min","hourly"]):  return "5d","60m","1H"
        if any(k in q for k in ["6 month","6mo","6m"]):         return "6mo","1d","6M"
        if any(k in q for k in ["3 month","3mo"]):              return "3mo","1d","3M"
        if any(k in q for k in ["2 year","2y"]):                return "2y","1d","2Y"
        return "1y","1d","1D"

    # ── Data bundle ────────────────────────────────────────────────────────────

    def _fetch_bundle(self, sym: str) -> dict | None:
        """
        Fetch + compute everything needed to answer any question about `sym`:
        fundamentals, 1Y technicals/levels (short-term), 2Y technicals/levels
        (long-term — genuinely separate data, not the same pivot set reused),
        risk, sentiment, quarterly results, recent news. Returns None if the
        ticker has no real market data.
        """
        info = get_info(sym)
        comp = info.get("longName") or info.get("shortName") or sym
        if not info or (info.get("regularMarketPrice") is None and info.get("currentPrice") is None):
            return None

        with ThreadPoolExecutor(max_workers=6) as exe:
            f_ohlcv_1y = exe.submit(get_ohlcv, sym, "1y", "1d")
            f_ohlcv_2y = exe.submit(get_ohlcv, sym, "2y", "1d")
            f_fins     = exe.submit(get_financials, sym)
            f_macro    = exe.submit(get_macro)
            f_qtext    = exe.submit(get_latest_quarterly_results, sym)
            f_news     = exe.submit(company_news, sym, comp)

        df_1y  = f_ohlcv_1y.result()
        df_2y  = f_ohlcv_2y.result()
        fins   = f_fins.result()
        macro  = f_macro.result()
        q_text = f_qtext.result()
        news   = f_news.result()

        df_ta_1y = compute_all(df_1y.copy()) if not df_1y.empty else df_1y
        df_ta_2y = compute_all(df_2y.copy()) if not df_2y.empty else df_2y
        ta_1y = snapshot(df_ta_1y)
        ta_2y = snapshot(df_ta_2y) if not df_ta_2y.empty else {}
        sr_1y = support_resistance(df_ta_1y)
        sr_2y = support_resistance(df_ta_2y) if not df_ta_2y.empty else {}

        fund = compute(info, fins)
        price = ta_1y.get("current_price") or fund.get("price")
        if price:
            fund["price"] = price

        sent = analyse(news)
        risk = assess(fund, ta_1y, sent["score"], macro)
        q_note = trim_text(q_text, 120) if q_text and "No " not in q_text else "Not available via scraper"

        s1 = sr_1y.get("S1"); r1 = sr_1y.get("R1")
        st_rr = compute_rr(price, s1, r1, price) if all(x for x in [price, s1, r1]) else {"valid": False, "error": "Insufficient level data"}

        # NOTE: support_resistance()'s classic pivot formula (Pivot=(H+L+C)/3)
        # only reads the LAST row of the input df — it is insensitive to
        # whether 1Y or 2Y of history was fetched, so sr_2y's S2/R2 would be
        # nearly identical to sr_1y's. For a genuinely different long-term
        # view, use the 2Y swing highs/lows instead — swing_levels() scans the
        # entire input range, so a 2Y df produces materially wider structural
        # levels than a 1Y df.
        sw2_res = sr_2y.get("swing_highs", [])
        sw2_sup = sr_2y.get("swing_lows", [])
        lt_target = max(sw2_res) if sw2_res else sr_2y.get("R2") or sr_1y.get("R2")
        lt_stop   = min(sw2_sup) if sw2_sup else sr_2y.get("S2") or sr_1y.get("S2")
        lt_rr = compute_rr(price, lt_stop, lt_target, price) if all(x for x in [price, lt_stop, lt_target]) else {"valid": False, "error": "Insufficient level data"}

        return {
            "sym": sym, "comp": comp, "price": price, "fund": fund,
            "ta_1y": ta_1y, "sr_1y": sr_1y, "ta_2y": ta_2y, "sr_2y": sr_2y,
            "lt_stop": lt_stop, "lt_target": lt_target,
            "risk": risk, "sent": sent, "q_note": q_note, "news": news,
            "st_rr": st_rr, "lt_rr": lt_rr, "df_ta_1y": df_ta_1y,
        }

    def _bundle_text(self, b: dict) -> str:
        """Render one ticker's data bundle into the text block fed to the LLM."""
        fund = b["fund"]; ta = b["ta_1y"]; sr = b["sr_1y"]; sr2 = b["sr_2y"]
        risk = b["risk"]; sent = b["sent"]
        fib  = ta.get("fib", {})
        pats = ta.get("patterns", [])
        news_titles = "\n".join(f"- {n['title']}" for n in b["news"][:6]) or "No recent news"
        pats_str = "; ".join(pats) if pats else "No significant signals"
        stdir = "BULLISH" if (ta.get("Supertrend_Dir") or 0) > 0 else "BEARISH"

        lines = [
            f"=== {b['comp']} ({b['sym']}.NS) ===",
            f"CMP ₹{b['price']}",
            "",
            "--- Fundamentals ---",
            to_text(fund),
            "",
            "--- Technicals (1Y / SHORT-TERM) ---",
            f"RSI {ta.get('RSI')} | MACD {ta.get('MACD')}/{ta.get('MACD_Signal')} | ADX {ta.get('ADX')} | "
            f"Supertrend {stdir} | ATR {ta.get('ATR')}",
            f"SMA 20/50/100/200: {ta.get('SMA_20')}/{ta.get('SMA_50')}/{ta.get('SMA_100')}/{ta.get('SMA_200')}",
            f"1Y Pivot levels: S2={sr.get('S2')} S1={sr.get('S1')} Pivot={sr.get('Pivot')} R1={sr.get('R1')} R2={sr.get('R2')}",
            f"Fibonacci (52w): 61.8%={fib.get('0.618')} 50%={fib.get('0.500')} 38.2%={fib.get('0.382')}",
            f"Signals: {pats_str}",
            f"SHORT-TERM trade setup (1Y-based, Stop=S1, Target=R1): {b['st_rr'].get('formula', 'unavailable')}",
            "",
            "--- Technicals (2Y / LONG-TERM) ---",
            (f"2Y structural swing levels — Resistance(s): {sr2.get('swing_highs') or 'none detected'} | "
             f"Support(s): {sr2.get('swing_lows') or 'none detected'}"
             if sr2 else "2Y data unavailable"),
            f"LONG-TERM trade setup (2Y structural swing high/low — NOT the same-day pivot formula, "
            f"Stop={b.get('lt_stop')}, Target={b.get('lt_target')}): {b['lt_rr'].get('formula', 'unavailable')}",
            "",
            "--- Risk & Sentiment ---",
            f"Risk Score: {risk.get('score','N/A')}/100 — {risk.get('level','N/A')}",
            f"Risk Flags: {'; '.join(risk.get('flags', [])[:4]) or 'None'}",
            f"Sentiment: {sent.get('label','N/A')} (score {sent.get('score',0.0):+.3f})",
            "",
            "--- Quarterly Results ---",
            b["q_note"],
            "",
            "--- Recent News ---",
            news_titles,
        ]
        return "\n".join(lines)

    def _options_block(self, sym: str) -> str:
        try:
            opt = get_options(sym)
            if not opt["available"]:
                return "\n--- Options Chain ---\nNo options data available.\n"
            calls = opt["calls"][["strike","lastPrice","openInterest","impliedVolatility"]].head(8)
            puts  = opt["puts"][["strike","lastPrice","openInterest","impliedVolatility"]].head(8)
            return (
                f"\n--- Options Chain (Expiry {opt['expiry']}) ---\n"
                f"CALLS (top 8 by OI):\n{calls.to_string(index=False)}\n\n"
                f"PUTS (top 8 by OI):\n{puts.to_string(index=False)}\n"
            )
        except Exception as e:
            return f"\n--- Options Chain ---\nUnavailable: {e}\n"

    # ── Complexity & reliability helpers ────────────────────────────────────────

    def _is_complex(self, msg: str, is_compare: bool) -> bool:
        """
        Heuristic for whether a question bundles multiple sub-asks and needs
        more output room + explicit "cover every part" guidance. Comparisons
        are always complex; otherwise look for multiple question marks or
        conjunction words joining separate asks.
        """
        if is_compare:
            return True
        q = msg.lower()
        if q.count("?") >= 2:
            return True
        conj_hits = sum(q.count(w) for w in [" and ", " also ", " as well as ", "; ", " plus "])
        return conj_hits >= 1 and len(msg) > 45

    _NUM_RE = re.compile(r'-?\d+(?:,\d{3})*(?:\.\d+)?')

    def _grounding_note(self, llm_out: str, data_text: str) -> str:
        """
        Lightweight hallucination guardrail: every numeric claim in the answer
        should trace back to a number that actually appears in the data block
        fed to the LLM. Not a proof of correctness (numbers can coincidentally
        match), but catches the common failure mode of an invented figure.
        Flags for a human glance rather than silently trusting the model.
        """
        data_nums = {n.replace(",", "") for n in self._NUM_RE.findall(data_text)}
        out_nums  = [n.replace(",", "") for n in self._NUM_RE.findall(llm_out)]
        out_nums  = [n for n in out_nums if len(n.replace(".", "")) >= 2]
        if not out_nums:
            return ""
        unmatched = [n for n in out_nums if n not in data_nums]
        # Small numbers (ratios, percentages, single digits) are noisy to check —
        # only flag when a meaningful share of *substantial* figures don't trace back.
        if len(out_nums) >= 4 and len(unmatched) / len(out_nums) > 0.5:
            return (
                "\n\n---\n*⚠️ Some figures above could not be automatically verified "
                "against the source data bundle — cross-check before acting on them.*"
            )
        return ""

    # ── Unified answer ─────────────────────────────────────────────────────────

    def _answer(self, tickers: list[str], msg: str) -> tuple[str, str]:
        """
        Answers any question about 1+ tickers: fetches the full data bundle for
        each, always includes the verbatim user question in the prompt, and
        lets the LLM decide response shape (direct answer first, full report
        structure only for generic asks) instead of routing to a rigid template.
        """
        bundles = []
        errors  = []
        with ThreadPoolExecutor(max_workers=max(len(tickers), 1)) as exe:
            futs = {exe.submit(self._fetch_bundle, t): t for t in tickers}
            for fut in futs:
                t = futs[fut]
                try:
                    b = fut.result()
                    if b: bundles.append(b)
                    else: errors.append(f"{t}: no market data")
                except Exception as e:
                    errors.append(f"{t}: {e}")

        if not bundles:
            detail = "; ".join(errors) if errors else "unknown error"
            return f"⚠️ Could not fetch data for {', '.join(tickers)}.\n\nDetails: {detail}\n\nVerify tickers are valid NSE symbols.", None

        order = {t: i for i, t in enumerate(tickers)}
        bundles.sort(key=lambda b: order.get(b["sym"], 99))
        is_compare = len(bundles) > 1

        options_block = ""
        if not is_compare and any(k in msg.lower() for k in ["option","options chain","call","put","oi","open interest"]):
            options_block = self._options_block(bundles[0]["sym"])

        data_text = "\n\n".join(self._bundle_text(b) for b in bundles)

        guidance = (
            "This is a COMPARISON — weigh both stocks against each other on every dimension "
            "relevant to the question, then give a clear preference."
            if is_compare else "Answer for this single stock."
        )

        complex_q = self._is_complex(msg, is_compare)
        if complex_q:
            guidance += (
                " This question has multiple distinct parts — silently list them out, then make "
                "sure your answer explicitly covers every one before finishing. Use short headers "
                "or bullets per part if there are 3 or more."
            )

        prompt = (
            f'USER QUESTION: "{msg}"\n\n'
            f"Answer this question directly and specifically FIRST, citing exact numbers from "
            f"the data below — never invent numbers, never give vague ranges. {guidance}\n"
            f"Then add ONLY the supporting sections that are relevant to what was actually "
            f"asked — skip sections that don't help answer this specific question.\n"
            f"If the question is broad/generic (e.g. \"analyse X\", \"tell me about X\"), use "
            f"the full structure instead: Executive Summary, Investment Scorecard, Fundamental "
            f"Deep Dive, Technical Analysis (clearly separate SHORT-TERM/1Y levels from "
            f"LONG-TERM/2Y levels — they are different numbers, never conflate them), News & "
            f"Sentiment, Risk Assessment, Catalysts, Verdict.\n\n"
            f"=== DATA ===\n{data_text}\n{options_block}\n"
            f"*ARIA Equity Research | {now_ist()} | {self.provider}*"
        )

        max_tokens = TOKENS_COMPLEX if complex_q else TOKENS_SYNTHESIS
        llm_out = self._chat(prompt, temperature=0.2, max_tokens=max_tokens)
        llm_out += self._grounding_note(llm_out, data_text)

        chart = None
        extra = ""
        if not is_compare:
            period, interval, tflabel = self._timeframe(msg)
            df_chart = get_ohlcv(bundles[0]["sym"], period, interval)
            df_ta_chart = compute_all(df_chart.copy()) if not df_chart.empty else bundles[0]["df_ta_1y"]
            chart = candlestick(df_ta_chart, bundles[0]["sym"], bundles[0]["comp"], tflabel)
            extra = "\n\n---\n\n" + indicator_breakdown(bundles[0]["ta_1y"]) + "\n\n" + calc_breakdown(bundles[0]["fund"])

        return llm_out + extra, chart

    # ── Backtest & market overview (kept separate — different data shapes) ─────

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
            return self._chat(prompt, temperature=0.3, max_tokens=TOKENS_AGENT)
        except Exception as e:
            return f"⚠️ Market overview error: {e}"

    # ── Portfolio ──────────────────────────────────────────────────────────────

    def _port_add(self, msg: str) -> str:
        m = re.search(r'\b([A-Z]{2,12})\b.*?(\d+(?:\.\d+)?)\s*(?:shares?|qty)?\s*(?:at|@|for|₹|rs)?\s*(\d+(?:\.\d+)?)', msg.upper())
        if m:
            return port_add(self._user_id, clean_ticker(m.group(1)), float(m.group(2)), float(m.group(3)))
        return "⚠️ Format: `add TCS 10 shares at 3500`"

    def _port_remove(self, msg: str) -> str:
        m = re.search(r'\b([A-Z]{2,12})\b', msg.upper())
        return port_remove(self._user_id, clean_ticker(m.group(1))) if m else "⚠️ Specify ticker to remove."

    def _port_view(self) -> tuple[str, str]:
        from tools.charts.builder import portfolio_chart
        holdings, summary = get_live(self._user_id)
        chart = portfolio_chart(holdings)
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
            raw = self._ask(prompt, temperature=0.8, max_tokens=TOKENS_FAST)
            raw = raw.strip().strip("```json").strip("```").strip()
            sug = json.loads(raw)
            if isinstance(sug, list): return [str(s) for s in sug[:4]]
        except Exception:
            pass
        return ["Show price chart", "Check fundamentals", "Latest news", "Backtest strategy"]

    def reset(self):
        self._client.reset()
        self._last_symbol = None

    def load_history(self, messages: list[dict]):
        """Resumes a previously saved chat: seeds LLM memory and re-anchors
        the last-mentioned ticker so follow-up questions still work."""
        self._client.load_history(messages)
        for m in reversed(messages):
            valid = [t for t in extract_all_entities(m["content"]) if validate_ticker(t)]
            if valid:
                self._last_symbol = valid[-1]
                break
