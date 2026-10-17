# Security policy

## Reporting a vulnerability

Please report security problems privately. Don't open a public issue.

- Use GitHub's private vulnerability reporting: go to the repository's **Security** tab and choose **Report a vulnerability**. Only the maintainers see the report.
- If that isn't available to you, open a public issue that says only "security contact needed", with no details, and a maintainer will reach out.

Please include what you found, the steps to reproduce it, and the impact you expect. Leave out any real user data.

What to expect:
- An acknowledgement within 3 working days.
- A first assessment within 7 days.
- A fix or mitigation plan for confirmed issues, then credit in the changelog if you'd like it.

ARIA is a non-commercial project with no bug bounty.

Please don't:
- access or change other users' data;
- run denial-of-service or volume tests against an instance you don't operate;
- use social engineering.

Good-faith research that follows these rules is welcome.

## Supported versions

Only the latest release (`:stable` image, see docs/RELEASING.md) and `main` receive fixes.

## What's in scope

- The web app and API: `server.py`, `web/`, `app.py`.
  - Authentication and sessions.
  - Tenant isolation between users.
  - Stored BYO LLM keys.
  - Download routes.
- Prompt injection that makes ARIA run a write tool (an alert or a screener save) the user didn't ask for, or leaks another user's data.
- The worker and its jobs, the Telegram linking flow, and the release pipeline.

Out of scope:
- Findings in third-party data sources (docs/DATA_SOURCES.md).
- Missing headers with no exploit.
- Self-XSS.
- Reports from automated scanners with no proof of impact.

## How ARIA protects users (summary)

| Area | Control | Where |
|---|---|---|
| Sessions | HttpOnly, SameSite=Lax, Secure cookie; only a sha256 of the token is stored; rotation on login; "log out other devices" | SEC-04, `web/session.py` |
| Passwords and accounts | salted PBKDF2-SHA256; per-email and per-IP throttling with backoff; optional CAPTCHA; email verification; reset tokens that are single-use, expire in 30 minutes and are stored hashed | SEC-05, SEC-06 |
| Secrets | Fernet encryption of BYO keys with rotation (`SECRET_KEY_PREVIOUS`); boot refuses weak keys; log redaction filter | SEC-11 |
| Input and output | input guard, URL safety (SSRF), HTML/Markdown escaping, untrusted-text wrapping in prompts, write tools gated on explicit intent | SEC-01, SEC-03, SEC-10 |
| Data | tenant-isolation tests for every user-scoped query; append-only audit log; data export and account deletion | COMP-03, COMP-04 |
| Supply chain | hashed lock files, non-root image pinned by digest, Trivy, pip-audit, bandit, gitleaks, Dependabot; release images carry an SBOM and provenance | SEC-14, TEST-07 |

The incident steps for operators are in docs/RUNBOOK.md, and breach response is in docs/PRIVACY.md.
