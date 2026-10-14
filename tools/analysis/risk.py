"""tools/analysis/risk.py — multi-factor risk engine"""

from config import RISK_HIGH_DE, RISK_HIGH_PE, RISK_LOW_CR, RISK_LOW_ROCE
from utils.helpers import safe_float


def assess(fund: dict, ta: dict, sentiment_score: float, macro: dict) -> dict:
    flags = []
    scores = {}

    def _parse_pct(v):
        if v is None or v == "N/A":
            return None
        try:
            f = float(str(v).replace("%", ""))
            return f * 100 if abs(f) <= 1.5 else f
        except (TypeError, ValueError):
            return None

    # Valuation risk
    pe = safe_float(fund.get("pe"))
    vs = 0
    if pe:
        if pe > RISK_HIGH_PE:
            vs = 80
            flags.append(f"🔴 Very high P/E ({pe:.1f}) — expensive vs market")
        elif pe > 40:
            vs = 50
            flags.append(f"🟠 Elevated P/E ({pe:.1f})")
        elif pe < 0:
            vs = 75
            flags.append("🔴 Negative P/E — losses reported")
        else:
            vs = 20
    scores["Valuation"] = vs

    # Leverage risk
    de = safe_float(fund.get("de"))
    ls = 0
    if de:
        if de > RISK_HIGH_DE * 2:
            ls = 90
            flags.append(f"🔴 Very high D/E ({de:.1f}) — debt overhang")
        elif de > RISK_HIGH_DE:
            ls = 60
            flags.append(f"🟠 High D/E ({de:.1f})")
        else:
            ls = 20
    scores["Leverage"] = ls

    # Liquidity risk
    cr = safe_float(fund.get("current_ratio"))
    qs = 0
    if cr:
        if cr < RISK_LOW_CR:
            qs = 80
            flags.append(f"🔴 Low current ratio ({cr:.2f}) — liquidity stress")
        elif cr < 1.5:
            qs = 40
            flags.append(f"🟠 Moderate current ratio ({cr:.2f})")
        else:
            qs = 10
    scores["Liquidity"] = qs

    # Profitability risk
    roce = safe_float(fund.get("roce"))
    roe = _parse_pct(fund.get("roe"))
    npm = _parse_pct(fund.get("net_margin"))
    ps = 0
    if roce and roce < RISK_LOW_ROCE:
        ps = max(ps, 70)
        flags.append(f"🔴 Low ROCE ({roce:.1f}%) — poor capital efficiency")
    if roe and roe < 10:
        ps = max(ps, 50)
        flags.append(f"🟠 Low ROE ({roe:.1f}%)")
    if npm and npm < 0:
        ps = max(ps, 85)
        flags.append("🔴 Negative net margin — loss-making")
    scores["Profitability"] = ps

    # Volatility risk
    atr = safe_float(ta.get("ATR"))
    price = safe_float(ta.get("current_price"))
    if price is None:
        price = safe_float(fund.get("price"))
    vs2 = 20
    if atr and price and price > 0:
        atr_pct = (atr / price) * 100
        if atr_pct > 5:
            vs2 = 80
            flags.append(f"🔴 High ATR ({atr_pct:.1f}%) — very volatile")
        elif atr_pct > 3:
            vs2 = 50
            flags.append(f"🟠 Moderate ATR ({atr_pct:.1f}%)")
    scores["Volatility"] = vs2

    # Sentiment risk
    ss = max(0, int((-sentiment_score + 1) / 2 * 100))
    scores["Sentiment"] = ss
    if sentiment_score < -0.3:
        flags.append(f"🔴 Bearish news sentiment ({sentiment_score:+.2f})")
    elif sentiment_score > 0.3:
        flags.append(f"🟢 Positive news sentiment ({sentiment_score:+.2f})")

    # Macro risk
    ms = 20
    for k, v in (macro or {}).items():
        chg = v.get("change", 0)
        if not isinstance(chg, float):
            continue
        if k == "USD/INR" and chg > 0.5:
            ms = max(ms, 55)
            flags.append(f"🟠 INR weakening (+{chg:.2f}%) — import cost pressure")
        if k == "Crude Oil" and chg > 3:
            ms = max(ms, 50)
            flags.append(f"🟠 Crude rising (+{chg:.2f}%) — energy/logistics risk")
    scores["Macro"] = ms

    weights = {
        "Valuation": 0.15,
        "Leverage": 0.20,
        "Liquidity": 0.15,
        "Profitability": 0.15,
        "Volatility": 0.15,
        "Sentiment": 0.10,
        "Macro": 0.10,
    }
    overall = round(sum(scores.get(k, 0) * w for k, w in weights.items()), 1)

    def _lvl(s):
        if s >= 65:
            return "🔴 Very High"
        if s >= 45:
            return "🟠 High"
        if s >= 25:
            return "🟡 Medium"
        return "🟢 Low"

    return {
        "score": overall,
        "level": _lvl(overall),
        "factors": scores,
        "flags": flags,
        "text": _fmt(overall, _lvl(overall), scores, flags),
    }


def _fmt(score, level, factors, flags) -> str:
    bars = {k: "█" * (v // 10) + "░" * (10 - v // 10) + f" {v}" for k, v in factors.items()}
    lines = (
        [
            f"**Risk Score:** {score}/100 — {level}",
            "",
            "**Factor breakdown:**",
        ]
        + [f"- {k}: {b}" for k, b in bars.items()]
        + [
            "",
            "**Flags:**",
        ]
        + ([f"- {f}" for f in flags] if flags else ["- ✅ No major flags"])
    )
    return "\n".join(lines)
