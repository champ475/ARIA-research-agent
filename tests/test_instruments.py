"""VAL-02: the instrument master and name resolution.

tests/data/entity_cases.csv holds 200 names — symbols in any case, aliases,
full names, typos, BSE codes, renamed and delisted companies, ambiguous
group names, made-up names and mutual-fund names that contain bank names.
Expected outcomes were written from knowledge of the companies, not by
running the resolver.
"""

import csv
from pathlib import Path

import pytest

from tools.data.instruments import master, resolve

with open(Path(__file__).parent / "data" / "entity_cases.csv", encoding="utf-8") as _f:
    CASES = list(csv.DictReader(_f))


def _correct(row: dict) -> bool:
    r = resolve(row["name"])
    if r.status != row["status"]:
        return False
    if row["status"] == "found":
        return r.symbol == row["symbol"]
    if row["status"] == "ambiguous" and row["symbol"]:
        return row["symbol"] in [c.symbol for c in r.candidates]
    return True


def test_table_has_200_cases_across_every_category():
    assert len(CASES) == 200
    cats = {row["category"] for row in CASES}
    assert {"typo", "bse_code", "renamed", "delisted", "ambiguous", "unknown", "mutual_fund"} <= cats


def test_at_least_98_percent_of_names_resolve_correctly():
    wrong = [(row["name"], row["status"], resolve(row["name"]).status) for row in CASES if not _correct(row)]
    accuracy = 1 - len(wrong) / len(CASES)
    assert accuracy >= 0.98, f"{accuracy:.1%} correct; wrong: {wrong}"


@pytest.mark.parametrize(
    "name,symbol",
    [("TVS MOTOR", "TVSMOTOR"), ("500325", "RELIANCE"), ("ZOMATO", "ETERNAL"), ("LTIM", "LTM")],
)
def test_headline_corrections(name, symbol):
    r = resolve(name)
    assert (r.status, r.symbol) == ("found", symbol)


def test_renamed_symbols_explain_themselves():
    assert "Eternal" in (resolve("ZOMATO").note or "")
    assert "TMPV" in (resolve("TATAMOTORS").note or "")


def test_ambiguous_names_list_choices_not_a_guess():
    r = resolve("HDFC")
    assert r.status == "ambiguous" and r.symbol is None
    assert {"HDFCBANK", "HDFCLIFE", "HDFCAMC"} <= {c.symbol for c in r.candidates}


def test_unknown_name_is_not_found_and_never_a_made_up_symbol():
    r = resolve("XYZQ")
    assert r.status == "not_found" and r.symbol is None
    assert all(c.symbol in master().by_symbol for c in r.candidates)


def test_every_curated_symbol_and_alias_target_is_listed():
    """Regression guard: "TVS MOTOR", "TATAMOTORS", "LTIM" sat in the curated
    universe long after they stopped being valid NSE symbols."""
    from config import STOCK_UNIVERSE
    from utils.helpers import TICKER_ALIASES

    listed = master().by_symbol
    assert [s for s in STOCK_UNIVERSE if s not in listed] == []
    assert [(k, v) for k, v in TICKER_ALIASES.items() if v not in listed] == []


def test_missing_master_file_degrades_to_curated_universe(monkeypatch, tmp_path):
    import tools.data.instruments as inst

    monkeypatch.setattr(inst, "NSE_FILE", tmp_path / "missing.csv")
    inst.reset_cache()
    try:
        assert resolve("TCS").symbol == "TCS"
        assert resolve("SBI").symbol == "SBIN"
    finally:
        monkeypatch.undo()
        inst.reset_cache()


def test_dockerignore_ships_the_instrument_master():
    text = (Path(__file__).parent.parent / ".dockerignore").read_text(encoding="utf-8")
    assert "!data/instruments/" in text
