# Changelog

All notable changes to ARIA are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.0.0]

The second generation of ARIA: a rebuilt research engine, an accounts-based web app, and the operational hardening to run it for other people.

### Added

- **Research engine.** A LangGraph pipeline (understand, plan, research in parallel, summarise, verify, answer) replaces the earlier agent router. Answers stream live and cite their evidence, with a confidence level when data is thin.
- **Verification.** Claims are checked against collected evidence. Bad claims are withheld or flagged, and ARIA asks before continuing on weak data.
- **Entity resolution.** Company names resolve against the full NSE and BSE instrument list. Guessed tickers are never trusted.
- **Equity research** with lender-aware fundamentals, technicals, news sentiment, peer comparison, risk profile and backtests.
- **Reports** (initiating coverage, result update, sector thematic, quick note) stored per account and downloadable as PDF or Markdown at any time.
- **Mutual fund** NAV lookup and category comparison with returns and risk metrics.
- **Stock screener** over a nightly snapshot of every NSE-listed stock, with saved screens and CSV export.
- **Alerts** with 14 condition types, delivered through Telegram.
- **Goal-based planning** with a remembered financial profile.
- **Portfolio** with Zerodha and Groww CSV import and live P&L, plus a **watchlist**.
- **Accounts.** Email and password sign-up with verification, password reset, session management, JSON data export and account deletion.
- **Bring your own LLM key** for any OpenAI-compatible provider, encrypted at rest, with a free daily allowance on the operator's keys.
- **Chat history** with rename, search, paging and Markdown export.
- **"Night Ledger" interface.** Dark by default with a light option, WCAG 2.1 AA contrast, keyboard navigation and text descriptions of charts.
- **Admin tab** for operators: user lookup, turns, alerts, account disabling and feature flags.
- **Education-only advice boundary** applied to every request and answer.
- **Operations.** Docker Compose deployment (app, worker, Postgres), health and readiness endpoints, Prometheus metrics with a Grafana dashboard, structured logs, tracing, ops alerts, runbook, SLOs and capacity plan.
- **Security.** Argon2id password hashing, HttpOnly session cookies, strict security headers, rate limits and abuse controls, audit log, tenant isolation tests.
- **Quality gates.** Unit, integration, edge-case and browser tests, a golden-set answer-quality eval, lint, type, dependency and security scans, and hashed lock files.
- Tagged-release workflow with an SBOM, build provenance, an offline smoke test and rollback.

### Changed

- Database schema is managed by Alembic migrations (`python scripts/migrate.py`), not by app start-up code.
- Reports and CSVs are generated on request from Postgres. Nothing depends on a replica's local disk.
- Configuration is typed and validated at boot, and the app refuses to start with a clear list of problems.
- Default models are Groq `gpt-oss-120b` and `gpt-oss-20b`, with Gemini as fallback.

[Unreleased]: https://github.com/champ475/ARIA-research-agent/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/champ475/ARIA-research-agent/releases/tag/v2.0.0
