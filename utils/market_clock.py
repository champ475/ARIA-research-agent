"""
utils/market_clock.py — the NSE trading calendar (DATA-06).

Answers "is the market open now", "which session do these figures belong
to" and "when does it next open", from the vendored holiday list in
data/market/nse_holidays.csv (weekends are closed; listed holidays are
closed; `special` rows add a session on an otherwise-closed day, such as
Muhurat trading). Every function takes an optional `now` so tests can pin
the clock; naive datetimes are read as IST.

Refresh the CSV each December from NSE's holiday circular. When the calendar
no longer covers the current year, `calendar_covers()` turns false and a
warning is logged once — weekdays are then assumed to be trading days.
"""

from __future__ import annotations

import csv
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path

from utils.helpers import IST

log = logging.getLogger(__name__)

CALENDAR_PATH = Path(__file__).resolve().parents[1] / "data" / "market" / "nse_holidays.csv"

PRE_OPEN = time(9, 0)
OPEN = time(9, 15)
CLOSE = time(15, 30)


@dataclass(frozen=True)
class SpecialSession:
    description: str
    open: time | None  # None: NSE hasn't announced the timing yet
    close: time | None


@dataclass(frozen=True)
class Calendar:
    holidays: dict[date, str]
    specials: dict[date, SpecialSession]
    last_year: int


@dataclass(frozen=True)
class SessionState:
    """state: open | pre_open | closed. reason (when closed): weekend |
    holiday | before_open | after_close. `holiday` names the occasion."""

    state: str
    reason: str | None = None
    holiday: str | None = None

    @property
    def label(self) -> str:
        if self.state == "open":
            return "market open"
        if self.state == "pre_open":
            return "pre-open session"
        if self.reason == "holiday" and self.holiday:
            return f"market closed — {self.holiday}"
        if self.reason == "weekend":
            return "market closed — weekend"
        return "market closed"


def _parse_time(s: str) -> time | None:
    s = (s or "").strip()
    if not s:
        return None
    h, m = s.split(":")
    return time(int(h), int(m))


@lru_cache(maxsize=1)
def load_calendar(path: Path = CALENDAR_PATH) -> Calendar:
    holidays: dict[date, str] = {}
    specials: dict[date, SpecialSession] = {}
    with path.open(encoding="utf-8") as f:
        rows = csv.DictReader(line for line in f if line.strip() and not line.startswith("#"))
        for row in rows:
            d = date.fromisoformat(row["date"].strip())
            if row["kind"].strip() == "special":
                specials[d] = SpecialSession(
                    row["description"].strip(), _parse_time(row["open"]), _parse_time(row["close"])
                )
            else:
                holidays[d] = row["description"].strip()
    years = [d.year for d in (*holidays, *specials)]
    return Calendar(holidays, specials, max(years) if years else 0)


_warned_years: set[int] = set()
_warn_lock = threading.Lock()


def calendar_covers(day: date | None = None) -> bool:
    """False once `day`'s year is past the last year in the CSV (logged once per year)."""
    day = day or _now().date()
    ok = day.year <= load_calendar().last_year
    if not ok:
        with _warn_lock:
            if day.year not in _warned_years:
                _warned_years.add(day.year)
                log.warning(
                    "NSE holiday calendar ends in %s; %s holidays are unknown — refresh data/market/nse_holidays.csv",
                    load_calendar().last_year,
                    day.year,
                )
    return ok


def _now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(IST)
    if now.tzinfo is None:
        return IST.localize(now)
    return now.astimezone(IST)


def _hours(day: date) -> tuple[time, time] | None:
    """(open, close) for `day`, or None when there's no session."""
    cal = load_calendar()
    special = cal.specials.get(day)
    if special is not None:
        return (special.open, special.close) if special.open and special.close else None
    if day.weekday() >= 5 or day in cal.holidays:
        return None
    calendar_covers(day)
    return OPEN, CLOSE


def is_trading_day(day: date) -> bool:
    """True when `day` has a session (a normal weekday, or a special one)."""
    return _hours(day) is not None


def session_state(now: datetime | None = None) -> SessionState:
    now = _now(now)
    day, t = now.date(), now.time()
    hours = _hours(day)
    if hours is None:
        cal = load_calendar()
        if day in cal.holidays:
            return SessionState("closed", "holiday", cal.holidays[day])
        if day in cal.specials:  # announced session, timing not out yet
            return SessionState(
                "closed", "holiday", f"{cal.specials[day].description}, timing not yet announced"
            )
        return SessionState("closed", "weekend")
    open_t, close_t = hours
    if open_t <= t <= close_t:
        return SessionState("open")
    holidays = load_calendar().holidays
    if day in holidays:  # a holiday with a special session, outside its hours
        return SessionState("closed", "holiday", holidays[day])
    if t < open_t:
        if day not in load_calendar().specials and t >= PRE_OPEN:
            return SessionState("pre_open")
        return SessionState("closed", "before_open")
    return SessionState("closed", "after_close")


def is_open(now: datetime | None = None) -> bool:
    return session_state(now).state == "open"


def last_session(now: datetime | None = None) -> date:
    """The session the latest market figures belong to: today once it has
    opened, otherwise the most recent earlier trading day."""
    now = _now(now)
    hours = _hours(now.date())
    if hours is not None and now.time() >= hours[0]:
        return now.date()
    day = now.date() - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(day):
            return day
        day -= timedelta(days=1)
    return day


def last_close(now: datetime | None = None) -> datetime | None:
    """When the most recent finished session closed (IST); None while a session
    is open or hasn't finished yet today."""
    now = _now(now)
    if session_state(now).state == "open":
        return None
    day = last_session(now)
    hours = _hours(day)
    ts = IST.localize(datetime.combine(day, hours[1] if hours else CLOSE))
    return ts if ts <= now else None


def next_open(now: datetime | None = None) -> datetime:
    """When the next session starts (IST); `now` itself if the market is open."""
    now = _now(now)
    day = now.date()
    for _ in range(30):
        hours = _hours(day)
        if hours is not None:
            start = IST.localize(datetime.combine(day, hours[0]))
            end = IST.localize(datetime.combine(day, hours[1]))
            if now <= end:
                return max(start, now)
        day += timedelta(days=1)
    return IST.localize(datetime.combine(day, OPEN))


def _day_label(d: date) -> str:
    return f"{d:%a}, {d.day} {d:%b %Y}"


def as_of_line(as_of: datetime | None, now: datetime | None = None) -> str:
    """'Data as of 15 Oct 2026, 15:31 IST (market closed)' for the oldest
    market figure in an answer; '' when there's nothing to date."""
    if as_of is None:
        return ""
    ts = _now(as_of)
    state = session_state(now)
    return f"Data as of {ts.day} {ts:%b %Y, %H:%M} IST ({state.label})"


def closed_today_note(now: datetime | None = None) -> str:
    """For 'today' questions on a day with no session: says the figures are
    the last session's. '' on trading days."""
    now = _now(now)
    if is_trading_day(now.date()):
        return ""
    state = session_state(now)
    why = f" ({state.holiday})" if state.holiday else ""
    return f"Markets are closed today{why}; these figures are from the last session, {_day_label(last_session(now))}."
