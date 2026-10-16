"""TEST-03: property tests for every parser and validator — they never raise
on arbitrary input, normalisation is idempotent, and formatting round-trips.
Examples per test: 500 in CI (profile "ci", tests/conftest.py)."""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from utils.helpers import IST

text = st.text(max_size=300)
noisy = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=300)


@given(noisy)
def test_input_guard_is_total_and_idempotent(raw):
    from utils.input_guard import CleanMessage, clean_message

    first = clean_message(raw)
    if isinstance(first, CleanMessage):
        again = clean_message(first.text)
        assert isinstance(again, CleanMessage) and again.text == first.text


@given(text, st.sampled_from(["TCS", "INFY", "RELIANCE"]))
def test_alert_parser_never_raises(msg, symbol):
    from graph.alert_spec import AlertRefusal, AlertSpec, parse_alert

    out = parse_alert(msg, symbol)
    assert out is None or isinstance(out, AlertSpec | AlertRefusal)


@given(text)
def test_confirmation_classifier_is_total(reply):
    from graph.confirmation import classify_reply

    assert classify_reply(reply) in ("yes", "no", "new_question", "unclear")


@given(text)
def test_build_trigger_and_amounts_never_raise(msg):
    from utils.financial_profile import wants_build
    from utils.numbers import find_numbers, parse_inr_amount

    wants_build(msg)
    parse_inr_amount(msg)
    find_numbers(msg)


@given(st.decimals(min_value=-(10**12), max_value=10**12, places=2, allow_nan=False, allow_infinity=False))
def test_inr_formatting_round_trips(value):
    from utils.numbers import format_inr, parse_inr_amount

    parsed = parse_inr_amount(format_inr(value))
    assert parsed is not None and parsed.value == value


@given(text)
def test_screener_grammar_never_raises(query):
    from tools.screener import as_screen, parse_query

    as_screen(parse_query(query)).summary()


@given(text)
def test_entity_extraction_never_raises(query):
    from graph.entity_resolution import resolve_mentions

    resolve_mentions(query)


@given(st.text(max_size=2000))
def test_portfolio_csv_parser_only_raises_user_errors(content):
    from tools.portfolio import parse_csv
    from utils.errors import UserInputError

    try:
        lots, problems = parse_csv(content)
    except UserInputError:
        return
    assert isinstance(lots, list) and isinstance(problems, list)


@given(st.datetimes(min_value=datetime(2024, 1, 1), max_value=datetime(2027, 12, 31)))
def test_market_clock_is_total_and_consistent(naive):
    from utils import market_clock as mc

    now = IST.localize(naive)
    state = mc.session_state(now)
    assert state.state in ("open", "pre_open", "closed")
    assert mc.is_open(now) == (state.state == "open")
    nxt = mc.next_open(now)
    assert nxt >= now - timedelta(seconds=1) and nxt - now < timedelta(days=15)
    assert mc.last_session(now) <= now.date()


@given(st.text(max_size=120))
def test_backtest_params_are_always_sane(query):
    from tools.analysis.backtester import parse_params

    p = parse_params(query)
    assert 1 <= p["years"] <= 10
    if p["strategy"] == "ma_crossover":
        assert 2 <= p["fast"] < p["slow"] <= 250
    else:
        assert 5 <= p["rsi_buy"] < p["rsi_sell"] <= 95
