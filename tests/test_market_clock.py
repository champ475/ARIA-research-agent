"""DATA-06: the NSE calendar on fixed dates — a holiday, a weekend, pre-open,
15:31 IST, Muhurat trading, a special Sunday session and an unannounced one."""

from __future__ import annotations

import logging
from datetime import date, datetime

import pytest

from utils import market_clock as mc
from utils.helpers import IST


def at(y, m, d, hh=12, mm=0):
    return IST.localize(datetime(y, m, d, hh, mm))


@pytest.mark.parametrize(
    "now, state, reason",
    [
        (at(2026, 10, 15, 11, 0), "open", None),  # Thursday, mid-session
        (at(2026, 10, 15, 9, 15), "open", None),  # the open itself
        (at(2026, 10, 15, 15, 30), "open", None),  # the close itself
        (at(2026, 10, 15, 15, 31), "closed", "after_close"),
        (at(2026, 10, 15, 9, 5), "pre_open", None),
        (at(2026, 10, 15, 8, 30), "closed", "before_open"),
        (at(2026, 10, 17, 11, 0), "closed", "weekend"),  # Saturday
        (at(2026, 10, 20, 11, 0), "closed", "holiday"),  # Dussehra (Tuesday)
        (at(2026, 1, 15, 11, 0), "closed", "holiday"),  # civic polls, added late
        (at(2025, 10, 21, 14, 0), "open", None),  # Muhurat, 13:45-14:45
        (at(2025, 10, 21, 11, 0), "closed", "holiday"),  # Diwali, outside Muhurat
        (at(2025, 10, 21, 14, 50), "closed", "holiday"),
        (at(2026, 2, 1, 11, 0), "open", None),  # Budget-day Sunday session
        (at(2026, 11, 8, 18, 0), "closed", "holiday"),  # Muhurat timing not announced
    ],
)
def test_session_state(now, state, reason):
    s = mc.session_state(now)
    assert (s.state, s.reason) == (state, reason)
    assert mc.is_open(now) is (state == "open")


def test_labels_name_the_holiday():
    assert mc.session_state(at(2026, 10, 20)).label == "market closed — Dussehra"
    assert mc.session_state(at(2026, 10, 18)).label == "market closed — weekend"
    assert "timing not yet announced" in mc.session_state(at(2026, 11, 8)).label
    assert mc.session_state(at(2026, 10, 15, 11)).label == "market open"


@pytest.mark.parametrize(
    "now, expected",
    [
        (at(2026, 10, 15, 11, 0), date(2026, 10, 15)),  # open: today
        (at(2026, 10, 15, 16, 0), date(2026, 10, 15)),  # after close: today
        (at(2026, 10, 15, 8, 0), date(2026, 10, 14)),  # before open: yesterday
        (at(2026, 10, 18, 11, 0), date(2026, 10, 16)),  # Sunday -> Friday
        (at(2026, 10, 21, 8, 0), date(2026, 10, 19)),  # skips Dussehra (Tue 20th)
        (at(2026, 2, 2, 8, 0), date(2026, 2, 1)),  # the Budget Sunday counts
        (at(2026, 1, 16, 8, 0), date(2026, 1, 14)),  # skips the poll holiday
    ],
)
def test_last_session(now, expected):
    assert mc.last_session(now) == expected


def test_next_open():
    assert mc.next_open(at(2026, 10, 15, 11, 0)) == at(2026, 10, 15, 11, 0)  # already open
    assert mc.next_open(at(2026, 10, 15, 16, 0)) == at(2026, 10, 16, 9, 15)
    assert mc.next_open(at(2026, 10, 17, 10, 0)) == at(2026, 10, 19, 9, 15)  # Sat -> Mon
    assert mc.next_open(at(2026, 10, 19, 16, 0)) == at(2026, 10, 21, 9, 15)  # over Dussehra
    assert mc.next_open(at(2025, 10, 21, 9, 0)) == at(2025, 10, 21, 13, 45)  # Muhurat


def test_naive_datetimes_are_ist():
    assert mc.is_open(datetime(2026, 10, 15, 11, 0))
    assert not mc.is_open(datetime(2026, 10, 15, 16, 0))


def test_as_of_line_and_closed_note():
    line = mc.as_of_line(at(2026, 10, 15, 15, 31), now=at(2026, 10, 15, 15, 40))
    assert line == "Data as of 15 Oct 2026, 15:31 IST (market closed)"
    assert mc.as_of_line(None) == ""
    note = mc.closed_today_note(at(2026, 10, 18, 11, 0))
    assert note == "Markets are closed today; these figures are from the last session, Fri, 16 Oct 2026."
    holiday = mc.closed_today_note(at(2026, 10, 20, 11, 0))
    assert "(Dussehra)" in holiday and "Mon, 19 Oct 2026" in holiday
    assert mc.closed_today_note(at(2026, 10, 15, 11, 0)) == ""


def test_calendar_runs_out_warns_once(caplog):
    mc._warned_years.discard(2099)
    with caplog.at_level(logging.WARNING, logger="utils.market_clock"):
        assert not mc.calendar_covers(date(2099, 3, 2))
        assert not mc.calendar_covers(date(2099, 3, 3))
        # Weekdays are assumed open past the calendar's end.
        assert mc.is_trading_day(date(2099, 3, 4))
    assert sum("refresh data/market/nse_holidays.csv" in r.getMessage() for r in caplog.records) == 1
    assert mc.calendar_covers(date(2026, 6, 1))


def test_calendar_file_is_well_formed():
    cal = mc.load_calendar()
    assert cal.last_year >= 2026
    assert all(d.weekday() < 5 for d in cal.holidays), "weekend holidays don't belong in the CSV"
    for s in cal.specials.values():
        assert (s.open is None) == (s.close is None)
        if s.open and s.close:
            assert s.open < s.close


def test_last_close_is_the_finished_sessions_close():
    sat = IST.localize(datetime(2026, 10, 17, 19, 30))  # Saturday evening
    assert mc.last_close(sat) == IST.localize(datetime(2026, 10, 16, 15, 30))
    after_close = IST.localize(datetime(2026, 10, 15, 18, 0))  # Thursday evening
    assert mc.last_close(after_close) == IST.localize(datetime(2026, 10, 15, 15, 30))


def test_last_close_is_none_while_a_session_is_open_or_not_yet_over():
    assert mc.last_close(IST.localize(datetime(2026, 10, 15, 11, 0))) is None
    assert mc.last_close(IST.localize(datetime(2026, 10, 15, 8, 0))) is not None  # yesterday's close
