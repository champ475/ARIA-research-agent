"""
config.py — Central configuration for ARIA v2 (ARCH-03).

Every environment-driven setting is a typed, validated field on `Settings`
(pydantic-settings). The module-level names below (`GROQ_MODEL`,
`DATABASE_URL`, ...) are re-exports of the loaded settings so existing
`from config import X` call sites keep working; static data (sector universe,
TA defaults, feeds) stays as plain constants.

Two levels of validation:
- type/range errors (e.g. `LLM_TIMEOUT_SECONDS=abc`) stop the import with one
  readable list of problems (SystemExit 2);
- deployment checks (`boot_problems()`: DATABASE_URL shape, SECRET_KEY, an LLM
  provider in prod) run at process start via `check_boot()` — app.py, the
  worker and scripts call it; tests don't.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    """All operator-tunable settings, read from the environment / `.env`."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    # ── Environment (OPS-01) ──────────────────────────────────────────────────
    APP_ENV: Literal["dev", "test", "staging", "prod"] = "dev"

    # ── Groq key rotation (up to 5 free keys) ─────────────────────────────────
    GROQ_API_KEY_1: str = ""
    GROQ_API_KEY_2: str = ""
    GROQ_API_KEY_3: str = ""
    GROQ_API_KEY_4: str = ""
    GROQ_API_KEY_5: str = ""
    # LLM-08: Groq retired llama-3.3-70b-versatile and llama-3.1-8b-instant on
    # 2026-08-16 and Google gemini-2.0-flash in 2026; these are their
    # recommended successors. Override per node with ARIA_MODEL_<NODE>.
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    GROQ_MODEL_FAST: str = "openai/gpt-oss-20b"

    # ── Gemini fallback ───────────────────────────────────────────────────────
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_MODEL_FAST: str = "gemini-2.5-flash-lite"
    PREFERRED_PROVIDER: Literal["groq", "gemini"] = "groq"

    # ── Local LLM fallback (any OpenAI-compatible server; empty = disabled) ───
    LOCAL_LLM_BASE_URL: str = ""
    LOCAL_LLM_MODEL: str = "local-fast"
    LOCAL_LLM_API_KEY: str = "local"
    LOCAL_LLM_MAX_TOKENS: int = Field(1024, ge=16, le=32768)
    LOCAL_LLM_TIMEOUT_SECONDS: float = Field(300, gt=0)

    # ── Timeouts and the turn deadline (REL-02) ───────────────────────────────
    LLM_TIMEOUT_SECONDS: float = Field(30, gt=0)
    LLM_STREAM_TIMEOUT_SECONDS: float = Field(15, gt=0)
    YF_TIMEOUT_SECONDS: float = Field(10, gt=0)
    TOOL_TIMEOUT_SECONDS: float = Field(25, gt=0)
    TURN_DEADLINE_SECONDS: float | None = Field(None, gt=0)
    BREAKER_FAILURES: int = Field(5, ge=1)
    BREAKER_WINDOW_SECONDS: float = Field(60, gt=0)
    BREAKER_COOLDOWN_SECONDS: float = Field(30, gt=0)
    IO_POOL_WORKERS: int = Field(64, ge=1)
    TOOL_POOL_WORKERS: int = Field(32, ge=1)

    # ── Prompt budgets ────────────────────────────────────────────────────────
    SYNTHESIS_EVIDENCE_CHAR_BUDGET: int = Field(12000, ge=1000)

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = ""
    # REL-05: a direct (non-pooler) Postgres URL for job advisory locks; empty = DATABASE_URL.
    JOBS_DATABASE_URL: str = ""
    # REL-05: run the background jobs (alerts, housekeeping, Telegram poller,
    # nightly refreshes) inside the app process. Default: on in dev, off
    # elsewhere (run `python worker.py`). Locks keep it safe either way.
    RUN_JOBS_IN_APP: bool | None = None
    # OPS-01: the production database's hostname(s), comma-separated. Any
    # other APP_ENV refuses to start against them (dev/staging never touch
    # real users' data). Set it in every non-prod env file.
    PROD_DATABASE_HOSTS: str = ""
    # OBS-03: bearer token Prometheus sends to GET /metrics. Required outside
    # dev/test — without it the route is off there (404).
    METRICS_TOKEN: str = ""
    # OBS-03 SLOs (docs/SLO.md): monthly availability and equity p95 time to first output.
    SLO_AVAILABILITY: float = Field(0.995, gt=0, lt=1)
    SLO_EQUITY_P95_TTFT_SECONDS: float = Field(15.0, gt=0)
    # OBS-04: where the ops watchdog pages (either or both), and how.
    OPERATOR_TELEGRAM_CHAT_ID: str = ""
    OPERATOR_EMAIL: str = ""
    RUNBOOK_URL: str = "https://github.com/champ475/ARIA-research-agent/blob/main/docs/RUNBOOK.md"
    ALERT_REMINDER_MINUTES: int = Field(60, ge=5)
    # Database plan size for the "DB size > 80%" alert (Neon free tier: 512 MB).
    DB_SIZE_LIMIT_MB: int = Field(512, ge=1)
    # REL-08: where scripts/backup_db.py writes dumps (copy them off the machine).
    BACKUP_DIR: str = "backups"
    DB_POOL_MAX: int = Field(20, ge=1, le=500)
    DB_POOL_ACQUIRE_TIMEOUT: float = Field(10, gt=0)
    DB_POOL_MIN: int = Field(2, ge=0)
    DB_CONNECT_TIMEOUT: int = Field(10, ge=1)
    # Server-side cap on one statement (REL-04); 0 disables it.
    DB_STATEMENT_TIMEOUT_MS: int = Field(5000, ge=0)
    RUN_MIGRATIONS_ON_START: bool = False

    # ── Secrets (SEC-11) ──────────────────────────────────────────────────────
    SECRET_KEY: str = ""
    # Old SECRET_KEY value(s), comma-separated, still accepted for decryption
    # while scripts/rotate_secret_key.py re-encrypts stored keys.
    SECRET_KEY_PREVIOUS: str = ""

    # ── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_BOT_USERNAME: str = ""
    ALERT_MAX_DELIVERY_ATTEMPTS: int = Field(5, ge=1)

    # ── Serving ───────────────────────────────────────────────────────────────
    CHAT_CONCURRENCY: int = Field(8, ge=1, le=256)
    QUEUE_MAX_SIZE: int = Field(100, ge=1)
    TRIAL_DAILY_LIMIT: int = Field(5, ge=0)
    # SEC-12: chat turns one user may start per minute (everyone, BYO or trial).
    TURNS_PER_MINUTE: int = Field(10, ge=1)
    # VAL-03: companies researched in one turn (tasks = companies x tools).
    MAX_ENTITIES_PER_TURN: int = Field(5, ge=1, le=20)
    # LLM-11: free-trial users' LLM tokens per day (input + output); 0 = no cap.
    TRIAL_DAILY_TOKENS: int = Field(60_000, ge=0)
    # UX-04: assumed cost per side of a backtest trade, % (brokerage, STT, charges, slippage).
    BACKTEST_COST_PCT: float = Field(0.15, ge=0, le=5)
    # DATA-05: the screener's snapshot is stale (note + ops alert) after this.
    FUNDAMENTALS_MAX_AGE_HOURS: int = Field(26, ge=1)
    # DATA-02: deterministic offline market data (e2e tests, demos).
    ARIA_FAKE_DATA: bool = False
    # Evidence confidence when a fallback source stood in (e.g. bhavcopy for a quote).
    DEGRADED_CONFIDENCE: float = Field(0.6, gt=0, lt=1)
    # Rupees per US dollar, for the per-turn cost estimate.
    USD_INR: float = Field(84.0, gt=0)
    # Skip Reflection's LLM call when every planned task already succeeded.
    REFLECTION_SKIP_WHEN_COMPLETE: bool = True
    BYO_ALLOW_PRIVATE_HOSTS: bool = False
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    HOST: str = "0.0.0.0"  # noqa: S104  # nosec B104 - container entrypoint; host/proxy controls exposure
    PORT: int = Field(7860, ge=1, le=65535)
    # Session cookie `Secure` flag (SEC-04); unset = on everywhere except dev/test.
    COOKIE_SECURE: bool | None = None
    # Public URL users reach the app at — links in emails point here (SEC-06).
    APP_BASE_URL: str = "http://localhost:7860"
    # Email (SEC-06): "console" logs messages (dev/test only); "smtp" sends them.
    MAIL_BACKEND: Literal["console", "smtp"] = "console"
    SMTP_HOST: str = ""
    SMTP_PORT: int = Field(587, ge=1, le=65535)
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_STARTTLS: bool = True
    # Block the app until the email address is verified; unset = on except dev/test.
    REQUIRE_EMAIL_VERIFICATION: bool | None = None
    # SEC-05: proxies whose X-Forwarded-For is trusted (comma-separated IPs/CIDRs).
    TRUSTED_PROXIES: str = ""
    # Optional Cloudflare Turnstile CAPTCHA after repeated failures (SEC-05).
    TURNSTILE_SITE_KEY: str = ""
    TURNSTILE_SECRET: str = ""

    # ── Tracing (OBS-02) ──────────────────────────────────────────────────────
    ARIA_TRACING: str = ""
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "aria"

    @field_validator("PREFERRED_PROVIDER", "LOG_LEVEL", "ARIA_TRACING", mode="before")
    @classmethod
    def _normalise_case(cls, v: object, info: ValidationInfo) -> object:
        if not isinstance(v, str):
            return v
        return v.strip().upper() if info.field_name == "LOG_LEVEL" else v.strip().lower()

    @field_validator("LOCAL_LLM_BASE_URL", "DATABASE_URL", "JOBS_DATABASE_URL", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @property
    def groq_api_keys(self) -> list[str]:
        keys = [
            self.GROQ_API_KEY_1,
            self.GROQ_API_KEY_2,
            self.GROQ_API_KEY_3,
            self.GROQ_API_KEY_4,
            self.GROQ_API_KEY_5,
        ]
        return [k for k in keys if k.strip()]

    @property
    def cookie_secure(self) -> bool:
        if self.COOKIE_SECURE is not None:
            return self.COOKIE_SECURE
        return self.APP_ENV not in ("dev", "test")

    @property
    def run_jobs_in_app(self) -> bool:
        if self.RUN_JOBS_IN_APP is not None:
            return self.RUN_JOBS_IN_APP
        return self.APP_ENV == "dev"

    @property
    def require_email_verification(self) -> bool:
        if self.REQUIRE_EMAIL_VERIFICATION is not None:
            return self.REQUIRE_EMAIL_VERIFICATION
        return self.APP_ENV not in ("dev", "test")

    @property
    def turn_deadline_seconds(self) -> float:
        # A local CPU model needs minutes per turn, so the default stretches.
        if self.TURN_DEADLINE_SECONDS is not None:
            return self.TURN_DEADLINE_SECONDS
        return 600.0 if self.LOCAL_LLM_BASE_URL else 90.0


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["ARIA configuration error: fix these environment variables:"]
    for err in exc.errors():
        name = ".".join(str(p) for p in err.get("loc", ())) or "(settings)"
        lines.append(f"  - {name}: {err.get('msg', 'invalid value')}")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings. Exits with a readable list on bad values."""
    try:
        return Settings()
    except ValidationError as exc:
        sys.stderr.write(_format_validation_error(exc) + "\n")
        raise SystemExit(2) from None


SECRET_KEY_PLACEHOLDER = "change_this_to_any_random_string"  # noqa: S105  # nosec B105 - the .env.example value we refuse
SECRET_KEY_MIN_LENGTH = 32


def secret_key_problems(secret: str) -> list[str]:
    """SEC-11: refuse a missing, copied-from-the-example or short SECRET_KEY."""
    hint = 'generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
    if not secret:
        return [f"SECRET_KEY is not set; {hint}"]
    if secret.strip() == SECRET_KEY_PLACEHOLDER:
        return [f"SECRET_KEY is still the placeholder from .env.example; {hint}"]
    if len(secret) < SECRET_KEY_MIN_LENGTH:
        return [f"SECRET_KEY must be at least {SECRET_KEY_MIN_LENGTH} characters (got {len(secret)}); {hint}"]
    return []


def db_host(url: str | None) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def prod_db_problem(s: Settings) -> str | None:
    """OPS-01: a non-production process pointed at the production database."""
    prod_hosts = {h.strip().lower() for h in s.PROD_DATABASE_HOSTS.split(",") if h.strip()}
    if s.APP_ENV == "prod" or not prod_hosts:
        return None
    for name, url in (("DATABASE_URL", s.DATABASE_URL), ("JOBS_DATABASE_URL", s.JOBS_DATABASE_URL)):
        if db_host(url) in prod_hosts:
            return (
                f"APP_ENV={s.APP_ENV} must not use the production database ({name} host is listed in "
                "PROD_DATABASE_HOSTS). Point it at this environment's own database or Neon branch."
            )
    return None


def boot_problems(s: Settings | None = None, *, serving: bool = True) -> list[str]:
    """Deployment checks a process must pass before it starts work.
    `serving=False` (alert checker, worker jobs) skips the LLM-provider check."""
    s = s or get_settings()
    problems: list[str] = []
    prod_db = prod_db_problem(s)
    if prod_db:
        problems.append(prod_db)
    url = s.DATABASE_URL
    if not url:
        problems.append("DATABASE_URL is not set (postgresql://user:pass@host/db).")
    elif not url.startswith(("postgresql://", "postgres://")):
        problems.append("DATABASE_URL must start with postgresql:// (got a different scheme).")
    elif "@" not in url or "/" not in url.split("@", 1)[1]:
        problems.append("DATABASE_URL must look like postgresql://user:pass@host/dbname.")
    if serving and s.APP_ENV != "test":
        problems.extend(secret_key_problems(s.SECRET_KEY))
    if serving and s.APP_ENV in ("staging", "prod"):
        if s.MAIL_BACKEND == "console":
            problems.append(
                "MAIL_BACKEND=console only logs emails; set MAIL_BACKEND=smtp and SMTP_* outside dev/test."
            )
        elif not (s.SMTP_HOST and s.SMTP_FROM):
            problems.append("MAIL_BACKEND=smtp needs SMTP_HOST and SMTP_FROM.")
        if not s.APP_BASE_URL.startswith("https://"):
            problems.append("APP_BASE_URL must be the public https:// address (links in emails use it).")
    if serving and s.APP_ENV == "prod" and not (s.groq_api_keys or s.GEMINI_API_KEY or s.LOCAL_LLM_BASE_URL):
        problems.append(
            "APP_ENV=prod needs at least one LLM provider (GROQ_API_KEY_1, GEMINI_API_KEY or LOCAL_LLM_BASE_URL)."
        )
    return problems


def check_boot(*, serving: bool = True) -> None:
    """Print every boot problem at once and exit non-zero if there are any."""
    problems = boot_problems(serving=serving)
    if problems:
        sys.stderr.write("ARIA cannot start:\n" + "\n".join(f"  - {p}" for p in problems) + "\n")
        raise SystemExit(2)


settings = get_settings()

# ── Re-exports (existing call sites import these names) ───────────────────────
APP_ENV = settings.APP_ENV
GROQ_API_KEYS = settings.groq_api_keys
GROQ_MODEL = settings.GROQ_MODEL  # 14,400 req/day per key
GROQ_MODEL_FAST = settings.GROQ_MODEL_FAST  # lighter, for fast calls
GEMINI_API_KEY = settings.GEMINI_API_KEY
GEMINI_MODEL = settings.GEMINI_MODEL
GEMINI_MODEL_FAST = settings.GEMINI_MODEL_FAST
PREFERRED_PROVIDER = settings.PREFERRED_PROVIDER

LOCAL_LLM_BASE_URL = settings.LOCAL_LLM_BASE_URL
LOCAL_LLM_MODEL = settings.LOCAL_LLM_MODEL
LOCAL_LLM_API_KEY = settings.LOCAL_LLM_API_KEY  # llama.cpp ignores it
# A small CPU model generates ~5 tok/s; cap its output and bound one call.
LOCAL_LLM_MAX_TOKENS = settings.LOCAL_LLM_MAX_TOKENS
LOCAL_LLM_TIMEOUT_SECONDS = settings.LOCAL_LLM_TIMEOUT_SECONDS

# Cloud LLM calls: total wait for a non-streaming reply, and the longest gap
# allowed between streamed chunks (which also bounds time-to-first-token).
LLM_TIMEOUT_SECONDS = settings.LLM_TIMEOUT_SECONDS
LLM_STREAM_TIMEOUT_SECONDS = settings.LLM_STREAM_TIMEOUT_SECONDS
# One yfinance call (it takes no timeout of its own; utils/resilience.py bounds the wait).
YF_TIMEOUT_SECONDS = settings.YF_TIMEOUT_SECONDS
# One research tool call (may make several source calls).
TOOL_TIMEOUT_SECONDS = settings.TOOL_TIMEOUT_SECONDS
# Whole chat turn: research stops dispatching new work at 70% of it, and the
# answer is built from whatever evidence arrived.
TURN_DEADLINE_SECONDS = settings.turn_deadline_seconds
RESEARCH_DEADLINE_FRACTION = 0.7
BREAKER_FAILURES = settings.BREAKER_FAILURES
BREAKER_WINDOW_SECONDS = settings.BREAKER_WINDOW_SECONDS
BREAKER_COOLDOWN_SECONDS = settings.BREAKER_COOLDOWN_SECONDS
IO_POOL_WORKERS = settings.IO_POOL_WORKERS
TOOL_POOL_WORKERS = settings.TOOL_POOL_WORKERS

# ── Token budgets (OUTPUT tokens only — none bound the prompt size) ───────────
TOKENS_SYNTHESIS = 2000  # full report synthesis
TOKENS_COMPLEX = 3000  # multi-part questions / comparisons — need more room to cover every sub-ask
TOKENS_AGENT = 800  # sub-agent calls
TOKENS_FAST = 200  # suggestions, tiny calls
LLM_TEMPERATURE = 0.3
MAX_RETRIES = 2  # per provider before fallback

# ── Input limits (VAL-01) ─────────────────────────────────────────────────────
MAX_CHAT_CHARS = 4000  # one chat message, after normalisation (utils/input_guard.py)

# ── Prompt INPUT budget (contrast with TOKENS_* above, which cap OUTPUT) ──────
# Bounds evidence embedded in Reflection/Verification prompts. An uncapped
# evidence_summary once reached 10-11k tokens against Groq's 6,000 TPM cap for
# the 8B model in ONE call; at ~4 chars/token, 6,000 chars ≈ 1,500 tokens.
EVIDENCE_PROMPT_CHAR_BUDGET = 6000
# LLM-02: the answer (synthesis) prompt's budget for rendered evidence rows.
SYNTHESIS_EVIDENCE_CHAR_BUDGET = settings.SYNTHESIS_EVIDENCE_CHAR_BUDGET

# How long a provider stays marked unavailable after a STRUCTURALLY zero quota
# (e.g. Gemini's "limit: 0") — agents/llm_client.py's _ProviderHealthCache.
PROVIDER_ZERO_QUOTA_TTL_SECONDS = 3600

# ── Serving / free trial ──────────────────────────────────────────────────────
CHAT_CONCURRENCY = settings.CHAT_CONCURRENCY  # chat turns at once, server-wide
QUEUE_MAX_SIZE = settings.QUEUE_MAX_SIZE
HOST = settings.HOST
PORT = settings.PORT
COOKIE_SECURE = settings.cookie_secure
APP_BASE_URL = settings.APP_BASE_URL.rstrip("/")
MAIL_BACKEND = settings.MAIL_BACKEND
SMTP_HOST = settings.SMTP_HOST
SMTP_PORT = settings.SMTP_PORT
SMTP_USER = settings.SMTP_USER
SMTP_PASSWORD = settings.SMTP_PASSWORD
SMTP_FROM = settings.SMTP_FROM
SMTP_STARTTLS = settings.SMTP_STARTTLS
REQUIRE_EMAIL_VERIFICATION = settings.require_email_verification
TRUSTED_PROXIES = [p.strip() for p in settings.TRUSTED_PROXIES.split(",") if p.strip()]
TURNSTILE_SITE_KEY = settings.TURNSTILE_SITE_KEY
TURNSTILE_SECRET = settings.TURNSTILE_SECRET
TRIAL_DAILY_LIMIT = settings.TRIAL_DAILY_LIMIT  # free messages/day on the operator's keys
TURNS_PER_MINUTE = settings.TURNS_PER_MINUTE
MAX_ENTITIES_PER_TURN = settings.MAX_ENTITIES_PER_TURN
TRIAL_DAILY_TOKENS = settings.TRIAL_DAILY_TOKENS
ARIA_FAKE_DATA = settings.ARIA_FAKE_DATA
FUNDAMENTALS_MAX_AGE_HOURS = settings.FUNDAMENTALS_MAX_AGE_HOURS
BACKTEST_COST_PCT = settings.BACKTEST_COST_PCT
DEGRADED_CONFIDENCE = settings.DEGRADED_CONFIDENCE
USD_INR = settings.USD_INR
REFLECTION_SKIP_WHEN_COMPLETE = settings.REFLECTION_SKIP_WHEN_COMPLETE

# LLM-11: list prices, US$ per 1M tokens (input, output), for the operator's
# trial keys. A BYO user's model costs the operator nothing (shown as 0).
MODEL_PRICES_USD_PER_M: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
}


def call_cost_inr(model: str | None, tokens_in: int, tokens_out: int) -> float:
    """Estimated rupee cost of one call on the operator's keys."""
    price = MODEL_PRICES_USD_PER_M.get(model or "")
    if price is None:
        return 0.0
    return round((tokens_in * price[0] + tokens_out * price[1]) / 1_000_000 * USD_INR, 6)


# ── Data ──────────────────────────────────────────────────────────────────────
NSE_SUFFIX = ".NS"
BSE_SUFFIX = ".BO"
DATA_DIR = "data"

# ── Database (Neon Postgres — shared by the app and the alert checker) ────────
DATABASE_URL = settings.DATABASE_URL
JOBS_DATABASE_URL = settings.JOBS_DATABASE_URL
METRICS_TOKEN = settings.METRICS_TOKEN
SLO_AVAILABILITY = settings.SLO_AVAILABILITY
SLO_EQUITY_P95_TTFT_SECONDS = settings.SLO_EQUITY_P95_TTFT_SECONDS
OPERATOR_TELEGRAM_CHAT_ID = settings.OPERATOR_TELEGRAM_CHAT_ID
OPERATOR_EMAIL = settings.OPERATOR_EMAIL
RUNBOOK_URL = settings.RUNBOOK_URL
ALERT_REMINDER_MINUTES = settings.ALERT_REMINDER_MINUTES
DB_SIZE_LIMIT_MB = settings.DB_SIZE_LIMIT_MB
BACKUP_DIR = settings.BACKUP_DIR
RUN_JOBS_IN_APP = settings.run_jobs_in_app
DB_POOL_MAX = settings.DB_POOL_MAX
DB_POOL_ACQUIRE_TIMEOUT = settings.DB_POOL_ACQUIRE_TIMEOUT
DB_POOL_MIN = settings.DB_POOL_MIN
DB_CONNECT_TIMEOUT = settings.DB_CONNECT_TIMEOUT
DB_STATEMENT_TIMEOUT_MS = settings.DB_STATEMENT_TIMEOUT_MS

# ── Crypto (encrypts users' BYO LLM keys before storage, utils/crypto.py) ────
SECRET_KEY = settings.SECRET_KEY
SECRET_KEY_PREVIOUS = settings.SECRET_KEY_PREVIOUS

# ── Telegram (one shared bot; each user links their own chat in Settings) ─────
TELEGRAM_BOT_TOKEN = settings.TELEGRAM_BOT_TOKEN
TELEGRAM_BOT_USERNAME = settings.TELEGRAM_BOT_USERNAME  # without the @, for building the deep link
ALERT_MAX_DELIVERY_ATTEMPTS = settings.ALERT_MAX_DELIVERY_ATTEMPTS  # Telegram retries before inbox

MACRO_TICKERS = {
    "Nifty 50": "^NSEI",
    "Sensex": "^BSESN",
    "Nifty Bank": "^NSEBANK",
    "USD/INR": "USDINR=X",
    "Crude Oil": "CL=F",
    "Gold": "GC=F",
    "10Y Bond": "^TNX",
}

NEWS_FEEDS = {
    "moneycontrol": "https://www.moneycontrol.com/rss/latestnews.xml",
    "economic_times": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "livemint": "https://www.livemint.com/rss/markets",
    "business_std": "https://www.business-standard.com/rss/markets-106.rss",
}

# ── NSE/BSE scraping ──────────────────────────────────────────────────────────
NSE_BASE_URL = "https://www.nseindia.com"
NSE_BHAVCOPY_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv"
NSE_BULK_DEALS_URL = "https://www.nseindia.com/api/bulk-deal-data"
NSE_CORPORATE_URL = "https://www.nseindia.com/api/corporates-corporateActions"

# ── TA defaults ───────────────────────────────────────────────────────────────
TA_RSI_PERIOD = 14
TA_MACD_FAST = 12
TA_MACD_SLOW = 26
TA_MACD_SIGNAL = 9
TA_BB_PERIOD = 20
TA_SMA_SHORT = 50
TA_SMA_LONG = 200

# ── Risk thresholds ───────────────────────────────────────────────────────────
RISK_HIGH_DE = 2.0
RISK_LOW_CR = 1.0
RISK_HIGH_PE = 60.0
RISK_LOW_ROCE = 10.0

# ── NSE sector universe (for screener & peer comparison) ─────────────────────
SECTOR_UNIVERSE = {
    "Technology": [
        "TCS",
        "INFY",
        "WIPRO",
        "HCLTECH",
        "TECHM",
        "LTM",  # LTIMindtree, renamed LTM Ltd
        "PERSISTENT",
        "COFORGE",
        "MPHASIS",
        "OFSS",
        "KPITTECH",
        "TATAELXSI",
    ],
    "Financial Services": [
        # NB: no bare "HDFC" — HDFC Ltd merged into HDFCBANK in 2023, and a
        # stale standalone entry here let extract_all_entities() validate
        # "HDFC" as a real ticker inside unrelated phrases like "HDFC
        # Flexicap" (a mutual fund), misrouting mf-domain queries. The
        # phrase "HDFC Bank" still resolves via TICKER_ALIASES above.
        "HDFCBANK",
        "ICICIBANK",
        "SBIN",
        "KOTAKBANK",
        "AXISBANK",
        "INDUSINDBK",
        "BAJFINANCE",
        "BAJAJFINSV",
        "ICICIGI",
        "HDFCLIFE",
        "SBILIFE",
    ],
    "Energy": [
        "RELIANCE",
        "ONGC",
        "BPCL",
        "IOC",
        "GAIL",
        "COALINDIA",
        "NTPC",
        "POWERGRID",
        "ADANIGREEN",
        "TATAPOWER",
        "ADANIPOWER",
        "CESC",
    ],
    "Consumer Cyclical": [
        "TITAN",
        "MARUTI",
        "TMPV",  # Tata Motors Passenger Vehicles — the old TATAMOTORS listing after the demerger
        "BAJAJ-AUTO",
        "EICHERMOT",
        "HEROMOTOCO",
        "TVSMOTOR",
        "M&M",
        "TRENT",
        "DMART",
    ],
    "Healthcare": [
        "SUNPHARMA",
        "DRREDDY",
        "CIPLA",
        "DIVISLAB",
        "APOLLOHOSP",
        "MANKIND",
        "TORNTPHARM",
        "LUPIN",
        "ALKEM",
        "IPCALAB",
    ],
    "Consumer Defensive": [
        "HINDUNILVR",
        "ITC",
        "NESTLEIND",
        "DABUR",
        "MARICO",
        "GODREJCP",
        "BRITANNIA",
        "TATACONSUM",
        "VBL",
        "RADICO",
    ],
    "Industrials": [
        "LT",
        "SIEMENS",
        "ABB",
        "BHEL",
        "HAL",
        "BEL",
        "CONCOR",
        "ADANIPORTS",
        "IRFC",
        "PFC",
        "RECLTD",
    ],
    "Metals & Mining": [
        "TATASTEEL",
        "HINDALCO",
        "JSWSTEEL",
        "SAIL",
        "VEDL",
        "NMDC",
        "HINDZINC",
        "NATIONALUM",
        "MOIL",
    ],
    "Real Estate": [
        "DLF",
        "GODREJPROP",
        "LODHA",
        "OBEROIRLTY",
        "PHOENIXLTD",
        "SOBHA",
        "BRIGADE",
        "PRESTIGE",
    ],
    "Telecom": [
        "BHARTIARTL",
        "IDEA",
        "INDUSTOWER",
        "TATACOMM",
    ],
}

# Flattened stock universe across all sectors — historically named NIFTY200 but
# is actually ~100 curated NSE stocks, not the NIFTY 200 index constituents.
STOCK_UNIVERSE = [s for stocks in SECTOR_UNIVERSE.values() for s in stocks]


# ── Model routing (LLM-08) ────────────────────────────────────────────────────
# Each LLM-backed node walks an ordered list of "provider:model" entries (the
# trial path; a BYO user's own model is always used for them). A model a
# provider has retired is detected (startup check, or its first 404) and
# skipped, so a deprecation degrades to the next entry instead of failing
# every request. Change a model without a code change:
#   ARIA_MODEL_PLANNER="groq:openai/gpt-oss-20b,gemini:gemini-2.5-flash"
#   ARIA_MODELS_FAST=... / ARIA_MODELS_SYNTHESIS=...   (whole classes)
MODEL_CLASSES: dict[str, list[str]] = {
    "fast": [f"groq:{GROQ_MODEL_FAST}", f"gemini:{GEMINI_MODEL_FAST}", f"gemini:{GEMINI_MODEL}"],
    "synthesis": [f"groq:{GROQ_MODEL}", f"gemini:{GEMINI_MODEL}", f"groq:{GROQ_MODEL_FAST}"],
}
NODE_MODEL_CLASS: dict[str, str] = {
    "query_analyzer": "fast",
    "planner": "fast",
    "reflection": "fast",
    "verification": "fast",
    "suggestions": "fast",
    "summary": "fast",
    "extract": "fast",
    "communication": "synthesis",
    "default": "synthesis",
}
MODEL_PROVIDERS = ("groq", "gemini")


def _parse_route(raw: str) -> list[tuple[str, str]]:
    route = []
    for item in raw.split(","):
        provider, _, model = item.strip().partition(":")
        if provider.strip().lower() in MODEL_PROVIDERS and model.strip():
            route.append((provider.strip().lower(), model.strip()))
    return route


def model_route(node: str | None) -> list[tuple[str, str]]:
    """Ordered (provider, model) candidates for a node on the trial path."""
    import os

    node = node or "default"
    override = os.getenv(f"ARIA_MODEL_{node.upper()}", "").strip()
    if override and _parse_route(override):
        return _parse_route(override)
    cls = NODE_MODEL_CLASS.get(node, "synthesis")
    cls_override = os.getenv(f"ARIA_MODELS_{cls.upper()}", "").strip()
    route = _parse_route(cls_override) if cls_override else _parse_route(",".join(MODEL_CLASSES[cls]))
    if PREFERRED_PROVIDER == "gemini":
        route = [r for r in route if r[0] == "gemini"] + [r for r in route if r[0] != "gemini"]
    return route


def all_routed_models() -> list[tuple[str, str]]:
    """Every distinct (provider, model) any node may use — for the startup check."""
    seen: list[tuple[str, str]] = []
    for node in NODE_MODEL_CLASS:
        for entry in model_route(node):
            if entry not in seen:
                seen.append(entry)
    return seen
