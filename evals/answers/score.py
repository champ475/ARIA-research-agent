"""
evals/answers/score.py — scores the answers a user would read (EVAL-01).

Per turn, a case may carry:
  facts         [{entity: [aliases], metric: regex, value, kind: pct|inr|ratio, tol, rel}]
                every figure the answer states for that metric (and entity) must
                be within tol of the reference ("correct or silent": a fact the
                answer leaves out is not wrong, it lowers fact_coverage)
  claims_false  regexes for comparative claims the evidence contradicts
  must_include  regexes the answer must contain
  must_not      regexes it must not contain
  max_chars     length cap (default by family)
  followup      true on the turn a follow-up case is judged by

Metrics (counts are absolute; the gate needs them at 0):
  wrong_figures, claim_errors, advice_breaches, missing_fixtures, crashes
  note_rate         % of answers carrying a trust note (unverified figures,
                    "could not fully confirm", cross-check unavailable, a
                    removed-sentence note, a claim check)
  fact_coverage     % of reference facts the answers stated
  check_pass        % of must_include / must_not checks that held
  followup_success  % of follow-up turns whose checks all held
  length_ok         % of answers within their length cap
"""

from __future__ import annotations

import re

from graph.grounding import _NUMBER, _parse, normalise

MAX_CHARS = {"concept": 2500, "tax": 2500, "advice": 2500, "followup": 3000}
DEFAULT_MAX_CHARS = 4000
MAX_DROP_POINTS = 2.0
NOTE_RATE_GATE = 10.0

TRUST_NOTES = {
    "unverified": re.compile(r"Unverified figures:"),
    "unconfirmed": re.compile(r"Could not fully confirm"),
    "judge_unavailable": re.compile(r"automatic cross-check was unavailable"),
    "rewrite": re.compile(r"sentence that read as .{0,80}was removed|was removed: \""),
    "claim_check": re.compile(r"⚠️ \*Check:"),
}
GAP_NOTE = re.compile(r"Data not available this time")

# Language that crosses the education-only boundary (CORR-04); a negation in the
# same sentence ("this is not a recommendation to buy") keeps it.
ADVICE_PATTERNS = {
    "upside": re.compile(
        r"\broom for (?:further |more )?upside\b|\bupside (?:potential|of|to)\b|\d+(?:\.\d+)?\s?% upside\b",
        re.I,
    ),
    "bullish_view": re.compile(r"\b(?:bullish|bearish)\b", re.I),
    "suitability": re.compile(
        r"\bsuitable (?:candidates? |choices? |options? |picks? )?for (?:a |your |the )?[\w\- ]{0,30}portfolio",
        re.I,
    ),
    "pick": re.compile(
        r"\b(?:good|great|strong) (?:buy|pick)\b|\btop picks?\b|\bmust[- ]buy\b|\battractive (?:entry|buy)",
        re.I,
    ),
    "call": re.compile(
        r"\byou should (?:buy|sell|invest in|accumulate|exit)\b|\b(?:i|we) recommend (?:buying|selling|investing)",
        re.I,
    ),
    "promise": re.compile(r"\bguaranteed? returns?\b|\brisk[- ]free returns?\b|\bsure[- ]shot\b", re.I),
}
_INDICATOR = re.compile(
    r"\b(?:macd|rsi|crossover|signal|candlestick|pattern|momentum|sma|ema|moving average|divergence)\b", re.I
)
_NEGATION = re.compile(
    r"\b(?:not|no|never|isn't|aren't|cannot|can't|don't|doesn't|won't|without|avoid)\b", re.I
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")
_SEPARATOR = re.compile(r":?-{2,}:?")


def notes(text: str) -> list[str]:
    return [kind for kind, rx in TRUST_NOTES.items() if rx.search(text)]


def advice_breaches(text: str) -> list[str]:
    found = []
    for sentence in _SENTENCE.split(text):
        if sentence.lstrip().startswith("ℹ️"):
            continue  # ARIA's own fixed notes and footers
        for kind, rx in ADVICE_PATTERNS.items():
            if not rx.search(sentence) or _NEGATION.search(sentence):
                continue
            if kind == "bullish_view" and _INDICATOR.search(sentence):
                continue
            found.append(f"{kind}: {sentence.strip()[:140]}")
    return found


def _figures(snippet: str, kind: str) -> list:
    out = []
    for m in _NUMBER.finditer(normalise(snippet)):
        fig = _parse(m)
        if fig is None:
            continue
        if kind == "pct" and not fig.percent:
            continue
        if kind == "inr" and not fig.rupees:
            continue
        if kind == "ratio" and (fig.percent or fig.rupees):
            continue
        out.append(fig)
    return out


def _alias_re(aliases: list[str]) -> re.Pattern:
    return re.compile(r"(?<![\w&])(?:" + "|".join(re.escape(a) for a in aliases) + r")(?![\w&])", re.I)


def fact_values(text: str, fact: dict, other_aliases: list[str]) -> list:
    """Every figure the answer states for this fact's metric (and entity)."""
    metric = re.compile(fact["metric"], re.I)
    kind = fact.get("kind", "ratio")
    entity = _alias_re(fact["entity"]) if fact.get("entity") else None
    others = _alias_re(other_aliases) if other_aliases else None
    values: list = []
    header: list[str] | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(_SEPARATOR.fullmatch(c) for c in cells if c):
                continue
            if header is None:
                header = cells
                continue
            if metric.search(cells[0]) and entity:
                # Transposed table: one metric per row, one company per column.
                for i, h in enumerate(header):
                    if i and entity.search(h) and i < len(cells):
                        values += _figures(cells[i], kind)[:1]
                continue
            if entity and not any(entity.search(c) for c in cells[:2]):
                continue
            for i, h in enumerate(header):
                if metric.search(h) and i < len(cells):
                    values += _figures(cells[i], kind)[:1]
            continue
        header = None
        for sentence in _SENTENCE.split(line):
            if entity and not entity.search(sentence):
                continue
            if entity and others and others.search(sentence):
                # Several companies in one sentence: read the figure right after this one's name.
                for m in entity.finditer(sentence):
                    window = sentence[m.end() : m.end() + 60]
                    if metric.search(sentence):
                        values += _figures(window, kind)[:1]
                continue
            for m in metric.finditer(sentence):
                values += _figures(sentence[m.end() : m.end() + 80], kind)[:1]
    return values


def _fact_ok(fig, fact: dict) -> bool:
    ref = float(fact["value"])
    if fact.get("rel"):
        return abs(fig.value - ref) <= abs(ref) * float(fact["rel"])
    tol = max(float(fact.get("tol", 0.05)), 0.5 * 10 ** (-fig.decimals))
    return abs(fig.value - ref) <= tol + 1e-9


def score_turn(case: dict, spec: dict, turn: dict) -> dict:
    text = turn["text"]
    facts = spec.get("facts") or []
    all_aliases = [a for f in facts for a in (f.get("entity") or [])]
    wrong, found = [], 0
    for fact in facts:
        own = set(fact.get("entity") or [])
        vals = fact_values(text, fact, [a for a in all_aliases if a not in own])
        found += bool(vals)
        wrong += [
            f"{fact.get('label') or fact['metric']}: said {v.raw}, reference {fact.get('shown') or fact['value']}"
            for v in vals
            if not _fact_ok(v, fact)
        ]
    claims = [rx for rx in spec.get("claims_false") or [] if re.search(rx, text, re.I)]
    checks = [(rx, bool(re.search(rx, text, re.I))) for rx in spec.get("must_include") or []]
    checks += [(f"not {rx}", not re.search(rx, text, re.I)) for rx in spec.get("must_not") or []]
    cap = spec.get("max_chars") or MAX_CHARS.get(case["family"], DEFAULT_MAX_CHARS)
    return {
        "wrong": wrong,
        "facts": len(facts),
        "facts_found": found,
        "claims": claims,
        "breaches": advice_breaches(text),
        "notes": notes(text),
        "gap_note": bool(GAP_NOTE.search(text)),
        "checks_failed": [name for name, ok in checks if not ok],
        "checks": len(checks),
        "length_ok": len(text) <= cap,
        "missing": bool(turn["misses"]),
        "crashed": text.startswith("[crashed:"),
    }


def _pct(ok: int, n: int) -> float:
    return round(100 * ok / n, 2) if n else 100.0


def report(cases: list[dict], results: list[dict]) -> dict:
    by_id = {r["id"]: r for r in results}
    turns: list[dict] = []
    failures: list[dict] = []
    followups = followups_ok = 0
    for case in cases:
        result = by_id[case["id"]]
        for i, (spec, turn) in enumerate(zip(case["turns"], result["turns"], strict=True)):
            s = score_turn(case, spec, turn)
            turns.append(s)
            ok = not (
                s["wrong"]
                or s["claims"]
                or s["breaches"]
                or s["checks_failed"]
                or s["missing"]
                or s["crashed"]
            )
            if spec.get("followup"):
                followups += 1
                followups_ok += ok
            problems = (
                {k: s[k] for k in ("wrong", "claims", "breaches", "checks_failed", "notes") if s[k]}
                | ({"missing_fixture": True} if s["missing"] else {})
                | ({"crashed": True} if s["crashed"] else {})
            )
            if problems:
                failures.append({"id": case["id"], "turn": i, "q": spec["q"], **problems})
    n = len(turns)
    metrics = {
        "wrong_figures": sum(len(t["wrong"]) for t in turns),
        "claim_errors": sum(len(t["claims"]) for t in turns),
        "advice_breaches": sum(len(t["breaches"]) for t in turns),
        "missing_fixtures": sum(t["missing"] for t in turns),
        "crashes": sum(t["crashed"] for t in turns),
        "note_rate": _pct(sum(bool(t["notes"]) for t in turns), n),
        "fact_coverage": _pct(sum(t["facts_found"] for t in turns), sum(t["facts"] for t in turns)),
        "check_pass": _pct(
            sum(t["checks"] - len(t["checks_failed"]) for t in turns), sum(t["checks"] for t in turns)
        ),
        "followup_success": _pct(followups_ok, followups),
        "length_ok": _pct(sum(t["length_ok"] for t in turns), n),
    }
    return {"cases": len(cases), "turns": n, "metrics": metrics, "failures": failures}


COUNTS = ("wrong_figures", "claim_errors", "advice_breaches", "missing_fixtures", "crashes")
GATE_ZERO = COUNTS


def gate_problems(report_: dict) -> list[str]:
    """The Phase 1 gate (PRD v3): zero wrong figures, claims and breaches, every
    reply recorded, notes on under 10% of answers."""
    m = report_["metrics"]
    problems = [f"{k} = {m[k]} (must be 0)" for k in GATE_ZERO if m[k]]
    if m["note_rate"] >= NOTE_RATE_GATE:
        problems.append(f"note_rate = {m['note_rate']}% (must be under {NOTE_RATE_GATE}%)")
    return problems


def regressions(report_: dict, baseline: dict) -> list[str]:
    problems = []
    for k, base in baseline.get("metrics", {}).items():
        now = report_["metrics"].get(k)
        if now is None:
            continue
        if k in COUNTS:
            if now > base:
                problems.append(f"{k}: {base} -> {now}")
        elif k == "note_rate":
            if now > base + MAX_DROP_POINTS:
                problems.append(f"{k}: {base} -> {now} (rose more than {MAX_DROP_POINTS} points)")
        elif now < base - MAX_DROP_POINTS:
            problems.append(f"{k}: {base} -> {now} (dropped more than {MAX_DROP_POINTS} points)")
    return problems


def markdown(report_: dict, baseline: dict | None) -> str:
    lines = ["| Metric | Baseline | This run |", "|---|---|---|"]
    for k, now in report_["metrics"].items():
        base = (baseline or {}).get("metrics", {}).get(k, "")
        lines.append(f"| {k} | {base} | {now} |")
    head = f"**Answer eval** ({report_['cases']} cases, {report_['turns']} answers)\n\n"
    fails = report_["failures"]
    tail = ""
    if fails:
        tail = (
            f"\n\n<details><summary>Failing answers ({len(fails)})</summary>\n\n"
            + "\n".join(_failure_line(f) for f in fails)
            + "\n\n</details>"
        )
    return head + "\n".join(lines) + tail


def _failure_line(f: dict) -> str:
    parts = []
    for k, v in f.items():
        if k in ("id", "turn", "q"):
            continue
        if isinstance(v, list):
            v = "; ".join(str(x)[:160] for x in v)
        parts.append(f"**{k}**: {v}" if v is not True else f"**{k}**")
    return f"- `{f['id']}`#{f['turn']} “{f['q']}” — " + " · ".join(parts)
