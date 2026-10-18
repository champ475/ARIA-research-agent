"""Grounding: a worked example in a concept answer is not a data claim (live smoke 2026-10-18);
Unicode signs and digit grouping (v3 CORR-02)."""

from __future__ import annotations

import pytest

from graph.data_questions import NO_LIVE_DATA_INSTRUCTION
from graph.grounding import check, extract_figures

# The models write minus as an en dash, a non-breaking hyphen, a figure dash or
# the real minus sign, and put narrow no-break spaces before "%" and inside numbers.
DASHES = ["–", "‑", "‒", "−"]
GROUPING = [" ", " ", " "]


@pytest.mark.parametrize("dash", DASHES)
def test_a_unicode_dash_before_a_number_is_a_minus(dash):
    assert check(f"{dash}4.54 %", "-4.54").unsupported == []
    assert check(f"{dash}4.54 %", "return -4.54%").unsupported == []
    assert extract_figures(f"Max drawdown {dash}20.73 %")[0].value == -20.73


def test_the_review_backtest_answer_has_no_unverified_figures():
    """2026-10-18 probe: a correct RSI backtest table was flagged on –4.54 %, –2.39 %, –20.73 %."""
    sources = "Total return -13.78%. CAGR -14.76%. Max drawdown -20.73%. Sharpe -0.71. Buy and hold -18.41%."
    answer = (
        "| Measure | Reliance |\n|---|---|\n| Total return (strategy) | –13.78 % |\n"
        "| CAGR (strategy) | –14.76 % |\n| Max drawdown | –20.73 % |\n| Sharpe ratio | –0.71 |\n\n"
        "Both delivered negative returns, with the strategy’s CAGR at –14.76 % and buy-and-hold at −18.41 %."
    )
    assert check(answer, sources).unsupported == []


@pytest.mark.parametrize(
    "text, numbers",
    [
        ("Capex of 150–200 crore", [150, 200]),
        ("52-week range: ₹965.12 – ₹1,508.00", [965.12, 1508.0]),  # found in the eval replay
        ("Growth of 12.5 % – 14.5 %", [12.5, 14.5]),
        ("Target ₹1,500 - ₹1,700", [1500, 1700]),
    ],
)
def test_a_dash_between_two_numbers_is_still_a_range(text, numbers):
    assert [f.number for f in extract_figures(text)] == numbers


def test_a_negative_rupee_amount_keeps_its_sign():
    assert extract_figures("Net cash flow –₹1,200 crore")[0].value == -1200e7


@pytest.mark.parametrize("space", GROUPING)
def test_no_break_spaces_inside_a_number_are_grouping(space):
    figs = extract_figures(f"Net profit ₹4{space}436 crore")
    assert [f.value for f in figs] == [4436e7]
    assert check(f"Net profit ₹4{space}436 crore", "net_income 44360000000").unsupported == []
    assert check(f"₹8{space}82{space}170 cr", "marketCap 8821701345280").unsupported == []


@pytest.mark.parametrize("label", ["SMA 200", "EMA 50", "DMA 200", "SMA(200)"])
def test_a_moving_average_period_is_not_a_figure(label):
    """Eval replay: '| SMA 200 | 1,344.06 |' was flagged on '200'."""
    assert [f.number for f in extract_figures(f"| {label} | 1,344.06 |")] == [1344.06]


def test_a_level_after_a_moving_average_is_still_a_figure():
    assert [f.number for f in extract_figures("Price is above its SMA 1,344.06")] == [1344.06]


def test_an_ordinary_space_between_numbers_is_not_grouping():
    assert [f.number for f in extract_figures("RSI 72 345.5 rupees")] == [72, 345.5]


def test_a_loss_stated_without_its_sign_matches_the_negative_source():
    assert check("The strategy had a maximum drawdown of 34.5%.", "max_drawdown -34.5%").unsupported == []
    assert check("Shares fell 4.2% today.", "change -4.2%").unsupported == []


def test_a_gain_is_not_matched_to_a_loss():
    assert check("Shares rose 4.2% today.", "change -4.2%").unsupported == ["4.2%"]


def test_an_invented_figure_is_still_flagged_after_normalising():
    assert check("Max drawdown –31.0 %", "max_drawdown -20.73%").unsupported == ["-31.0 %"]


def test_worked_example_in_a_concept_answer_is_not_flagged():
    """A P/E explanation carried 'Unverified figures: ₹500, 50, ₹200'."""
    prompt = NO_LIVE_DATA_INSTRUCTION + "user: What does a high P/E ratio mean?"
    answer = (
        "A high P/E means investors pay more per rupee of earnings.\n\n"
        "Example:\nIf Company A trades at ₹500 per share and earns ₹10 per share, its P/E is 50."
    )
    assert check(answer, prompt).unsupported == []


def test_the_same_sentence_is_still_checked_when_data_was_fetched():
    assert check("Reliance trades at ₹500 per share, P/E 50.", "Price 1211").unsupported


def test_a_figure_stated_as_current_data_is_still_flagged_in_a_concept_answer():
    prompt = NO_LIVE_DATA_INSTRUCTION + "user: How is TCS?"
    assert check("TCS trades at ₹2,465 today.", prompt).unsupported
