"""
scripts/load_test.py — concurrent-chat load test (PRD REL-01 acceptance).

Measures time-to-first-token (TTFT: first streamed answer text after the
loading frame) for one user alone, then for N users at once, and reports
p50/p95 for both. REL-01 passes when the concurrent p95 is at most 1.5x the
single-user p95, with no errors.

Run it against a STAGING deployment with its own database and LLM keys,
never production: it signs up N throwaway users (loadtest+<n>@example.com)
and every turn spends real LLM quota.

    python scripts/load_test.py --url http://localhost:7860 --users 20 \
        --password 'LoadTest-Passw0rd!'

Requires `gradio_client` (installed with gradio). Users sign up / log in
through the /signup and /login forms (SEC-04 cookie sessions); the session
cookie is then sent with every Gradio API call. The chat event's API name is
Gradio's default (the handler name); if the UI is refactored, pass --chat-api.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time

QUESTIONS = [
    "What is TCS's current price?",
    "Show RELIANCE technicals",
    "INFY fundamentals",
    "HDFCBANK RSI",
    "Is ITC overbought?",
]


def _last_message(update) -> str:
    """The newest chat message text in one streamed update (the Chatbot output is the one list
    of {role, content} dicts; its position moves when outputs change)."""
    for part in update if isinstance(update, (list, tuple)) else [update]:
        if isinstance(part, list) and part and isinstance(part[-1], dict) and "content" in part[-1]:
            content = part[-1]["content"]
            if isinstance(content, list):
                content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            return str(content)
    return ""


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def _session_cookie(url: str, email: str, password: str) -> str:
    """Sign up (or, if the account exists, log in) through the HTML forms and
    return the aria_session cookie value."""
    import re

    import requests

    s = requests.Session()
    for page, data in (("/signup", {"agree": "yes"}), ("/login", {})):
        html = s.get(url + page, timeout=30).text
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        if not m:
            continue
        s.post(
            url + page, data={"email": email, "password": password, "csrf": m.group(1), **data}, timeout=30
        )
        if s.cookies.get("aria_session"):
            return s.cookies["aria_session"]
    raise RuntimeError(f"could not log in as {email}")


def _one_user(
    url: str,
    idx: int,
    password: str,
    chat_api: str,
    ttfts: list[float],
    errors: list[str],
    start: threading.Barrier | None,
) -> None:
    from gradio_client import Client

    try:
        email = f"loadtest+{idx}@example.com"
        cookie = _session_cookie(url, email, password)
        client = Client(url, verbose=False, headers={"Cookie": f"aria_session={cookie}"})
        if start is not None:
            start.wait(timeout=120)
        question = QUESTIONS[idx % len(QUESTIONS)]
        t0 = time.monotonic()
        job = client.submit(question, api_name=chat_api)
        first = None
        for update in job:
            text = _last_message(update)
            if text and "aria-dot" not in str(text):
                first = time.monotonic() - t0
                break
        job.result()
        if first is None:
            errors.append(f"user {idx}: no answer text streamed")
        else:
            ttfts.append(first)
    except Exception as e:  # noqa: BLE001 — a load test reports every failure
        errors.append(f"user {idx}: {type(e).__name__}: {e}")


def _run(url: str, users: int, password: str, chat_api: str) -> tuple[list[float], list[str]]:
    ttfts: list[float] = []
    errors: list[str] = []
    barrier = threading.Barrier(users) if users > 1 else None
    threads = [
        threading.Thread(target=_one_user, args=(url, i, password, chat_api, ttfts, errors, barrier))
        for i in range(users)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return ttfts, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:7860")
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--baseline-runs", type=int, default=5)
    ap.add_argument("--password", required=True)
    ap.add_argument("--chat-api", default="/_respond")
    args = ap.parse_args()

    baseline: list[float] = []
    for i in range(args.baseline_runs):
        t, errs = _run(args.url, 1, args.password, args.chat_api)
        baseline += t
        for e in errs:
            print(f"baseline run {i}: {e}")
    concurrent, errors = _run(args.url, args.users, args.password, args.chat_api)

    b95, c95 = _percentile(baseline, 95), _percentile(concurrent, 95)
    print(
        f"single-user TTFT  p50={statistics.median(baseline) if baseline else float('nan'):.2f}s  p95={b95:.2f}s  (n={len(baseline)})"
    )
    print(
        f"{args.users}-user TTFT     p50={statistics.median(concurrent) if concurrent else float('nan'):.2f}s  p95={c95:.2f}s  (n={len(concurrent)})"
    )
    for e in errors:
        print("ERROR", e)
    ratio = c95 / b95 if b95 and b95 == b95 else float("inf")
    ok = not errors and ratio <= 1.5
    print(f"p95 ratio = {ratio:.2f}  ->  {'PASS' if ok else 'FAIL'} (target <= 1.5, no errors)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
