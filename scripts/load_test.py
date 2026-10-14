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

Requires `gradio_client` (installed with gradio). The app's event API names
are Gradio's defaults (the handler function names); if the UI is refactored,
pass --login-api / --chat-api.
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


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def _one_user(url: str, idx: int, password: str, login_api: str, chat_api: str,
              ttfts: list[float], errors: list[str], start: threading.Barrier | None) -> None:
    from gradio_client import Client

    try:
        client = Client(url, verbose=False)
        email = f"loadtest+{idx}@example.com"
        # Sign up (ignored if it exists), then log in; state lives in this client's session.
        try:
            client.predict(email, password, True, api_name="/_do_signup")
        except Exception:
            pass
        client.predict(email, password, True, api_name=login_api)
        if start is not None:
            start.wait(timeout=120)
        question = QUESTIONS[idx % len(QUESTIONS)]
        t0 = time.monotonic()
        job = client.submit(question, api_name=chat_api)
        first = None
        for update in job:
            history = update[2] if isinstance(update, (list, tuple)) and len(update) > 2 else None
            text = (history or [{}])[-1].get("content", "") if history else ""
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


def _run(url: str, users: int, password: str, login_api: str, chat_api: str) -> tuple[list[float], list[str]]:
    ttfts: list[float] = []
    errors: list[str] = []
    barrier = threading.Barrier(users) if users > 1 else None
    threads = [
        threading.Thread(target=_one_user, args=(url, i, password, login_api, chat_api, ttfts, errors, barrier))
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
    ap.add_argument("--login-api", default="/_do_login")
    ap.add_argument("--chat-api", default="/_respond")
    args = ap.parse_args()

    baseline: list[float] = []
    for i in range(args.baseline_runs):
        t, errs = _run(args.url, 1, args.password, args.login_api, args.chat_api)
        baseline += t
        for e in errs:
            print(f"baseline run {i}: {e}")
    concurrent, errors = _run(args.url, args.users, args.password, args.login_api, args.chat_api)

    b95, c95 = _percentile(baseline, 95), _percentile(concurrent, 95)
    print(f"single-user TTFT  p50={statistics.median(baseline) if baseline else float('nan'):.2f}s  p95={b95:.2f}s  (n={len(baseline)})")
    print(f"{args.users}-user TTFT     p50={statistics.median(concurrent) if concurrent else float('nan'):.2f}s  p95={c95:.2f}s  (n={len(concurrent)})")
    for e in errors:
        print("ERROR", e)
    ratio = c95 / b95 if b95 and b95 == b95 else float("inf")
    ok = not errors and ratio <= 1.5
    print(f"p95 ratio = {ratio:.2f}  ->  {'PASS' if ok else 'FAIL'} (target <= 1.5, no errors)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
