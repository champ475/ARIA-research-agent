"""A solar-equipment maker is filed under Energy, not under "IT stocks" (live smoke 2026-10-18)."""

from __future__ import annotations

from scripts.refresh_fundamentals import snapshot_row


def _info(sector, industry):
    return {
        "currentPrice": 100.0,
        "marketCap": 5e10,
        "trailingPE": 13.3,
        "sector": sector,
        "industry": industry,
        "longName": "Emmvee Photovoltaic Power",
    }


def test_solar_industry_overrides_the_technology_sector():
    row = snapshot_row("EMMVEE", "Emmvee", "INE0000", _info("Technology", "Solar"))
    assert row["sector"] == "Energy"
    assert row["industry"] == "Solar"


def test_other_technology_companies_keep_their_sector():
    row = snapshot_row("TCS", "TCS", "INE467B01029", _info("Technology", "Information Technology Services"))
    assert row["sector"] == "Technology"
