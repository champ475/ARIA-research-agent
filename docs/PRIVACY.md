# ARIA — Privacy & data protection (DPDP Act readiness)

Status: **DRAFT — to be reviewed by counsel before any public launch** (PRD COMP-03).
This is the operator-facing companion to the user-facing notice in `LEGAL.md`.
It records what personal data ARIA processes, why, where it goes, how long it
stays, and how each right under India's Digital Personal Data Protection Act,
2023 (and its Rules) is met. Keep it true: update it in the same PR as any
change to data flows (Definition of done #4).

## 1. Purposes

| Purpose | Data used | Basis |
|---|---|---|
| Account and login | email, password hash, sessions (hashed token, IP, user agent, last seen) | consent at signup |
| Email verification and password reset | email, single-use token hashes | consent; needed to secure the account |
| Answering research questions | chat messages, conversation history, selected companies | consent |
| Alerts and their delivery | alert conditions, Telegram chat id, in-app notifications | consent |
| Planning (model portfolios) | financial profile: age, capital, horizon, risk tolerance, goal | consent; education-only (`docs/ADVICE_POLICY.md`) |
| Watchlist, portfolio, saved screens, reports | symbols, holdings, filters, generated reports | consent |
| Bring-your-own LLM key | encrypted key, model, base URL | consent |
| Free-trial limits, abuse prevention | per-day counters, auth-attempt rows keyed by email/IP | legitimate use: security |
| Security and dispute investigation | `audit_log` (action, user id, IP, user agent) | legitimate use: security |
| Service operation | per-turn records (`turns`: latency, tokens, errors; no message text), JSON logs (redacted) | legitimate use: operation |

## 2. Data categories and where they live

All personal data is stored in the operator's Postgres database (Neon by default):
`users`, `sessions`, `auth_tokens`, `auth_attempts`, `chat_sessions`, `chat_history`,
`alerts`, `notifications`, `watchlist`, `portfolio`, `saved_screeners`, `reports`,
`financial_profiles`, `user_llm_config`, `telegram_link_codes`, `turns`, `audit_log`.

Not stored: card or bank details, government IDs, contacts, location.

## 3. Processor register

| Processor | What it receives | Why | Location |
|---|---|---|---|
| Neon (Postgres) | all stored data | database hosting | per Neon project region |
| Groq | chat text and research context for trial users | LLM answers | USA |
| Google (Gemini API) | same, when Groq is unavailable | LLM fallback | per Google region |
| User's own LLM provider (BYO key) | chat text and context for that user | LLM answers, chosen by the user | provider's |
| Telegram | alert messages for users who link a chat | alert delivery | Telegram's |
| SMTP provider (e.g. Gmail) | the user's email address, verification/reset links | transactional email | provider's |
| LangSmith (optional, `ARIA_TRACING`) | redacted per-turn traces | debugging | per account region |
| Cloudflare Turnstile (optional) | IP address and browser signals at login | bot protection | Cloudflare's |
| Market-data sources (Yahoo, NSE, BSE, AMFI, Google News) | ticker symbols only — no personal data | market data | — |

Transfers outside India: chat content goes to LLM providers outside India.
Counsel to confirm the notice and consent wording for cross-border transfer
under the DPDP Rules.

## 4. Retention

| Data | Kept for | Enforced by |
|---|---|---|
| Account and everything it owns | until the user deletes the account | `/account/delete` (one transaction) |
| Login sessions | until expiry (14 days) or logout | hourly housekeeping |
| Verification / reset tokens | 1 day after expiry | hourly housekeeping |
| `auth_attempts` | 1 day | hourly housekeeping |
| `audit_log` | 5 years (or as counsel advises); survives account deletion with the numeric user id only | append-only table (COMP-04); retention job: COMP-06 |
| Logs | per log sink (stderr by default); emails and secrets are redacted | `utils/logging_setup.py` |
| Backups | per the backup schedule (REL-08) | operator |

## 5. User rights and how they are met

| Right | How |
|---|---|
| Notice | `LEGAL.md`, shown at signup (`/terms`), versioned (COMP-02) |
| Consent and withdrawal | checkbox at signup; re-consent after material changes; withdrawal = deleting the account |
| Access | Settings → "download everything ARIA stores about you" (`/account/export`, JSON of every user-scoped table, secrets withheld) |
| Correction | profile/watchlist/alerts/settings editable in the app; email change: contact grievance officer (self-serve planned) |
| Erasure | Settings → delete my account (`/account/delete`) |
| Grievance | contact below; respond within the period the Rules require |

**Grievance contact:** `<operator name>`, `<email>` — operator to fill in before launch.

## 6. Data minimisation in practice

- Logs: email addresses, API keys, tokens and Telegram bot tokens are masked by a
  logging filter on every record (`utils/logging_setup.py`).
- Traces: `utils/tracing.redact_for_trace` masks secrets and emails and truncates text.
- Failed logins record a hash of the typed address, never the address itself.
- Audit details carry summaries (symbol, condition), never secrets.
- The export never returns password/token hashes or the encrypted API key.

## 7. Personal-data breach response (draft — counsel to confirm timelines)

1. **Contain** — rotate `SECRET_KEY` (`scripts/rotate_secret_key.py`), revoke sessions
   (`DELETE FROM sessions`), rotate database and provider credentials, disable affected features.
2. **Assess** — what data, how many users, since when (use `audit_log`, `turns`, provider logs).
3. **Notify the Data Protection Board of India** and **affected users** in the form and within
   the time the DPDP Rules require.
4. **Record** — a dated incident note in `docs/` with root cause and fixes.
5. **Review** — add a test or alert that would have caught it.
