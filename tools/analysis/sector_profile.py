"""
tools/analysis/sector_profile.py — which ratios make sense for a company (DATA-03).

For lenders (banks, NBFCs, housing-finance companies) borrowing is the raw
material, not a financing choice, and there are no current assets or
current liabilities in the industrial sense. So ROCE, D/E and the current
ratio say nothing useful about them and are suppressed; what matters is net
interest margin, asset quality (GNPA) and the deposit franchise (CASA),
which the current data source only partly carries.
"""

from __future__ import annotations

from typing import Any

LENDER = "lender"
GENERAL = "general"

# yfinance industry names for lending businesses.
_LENDER_INDUSTRIES = {
    "banks - regional",
    "banks - diversified",
    "credit services",
    "mortgage finance",
    "financial conglomerates",
}
_LENDER_WORDS = ("bank", "credit", "mortgage", "lending", "housing finance", "nbfc")

# Ratios that mislead for each profile.
SUPPRESSED: dict[str, frozenset[str]] = {
    LENDER: frozenset(
        {
            "roce",
            "de",
            "current_ratio",
            "total_debt",
            "fcf",
            "operating_cf",
            "op_margin",
            "ebitda",
            "ebitda_margin",
            "ev",
            "ev_ebitda",
            "current_assets",
            "current_liab",
        }
    ),
    GENERAL: frozenset(),
}

LENDER_NOTE = (
    "Not shown for lenders: ROCE, D/E, the current ratio, debt, free cash flow, EBITDA and operating margin. For a bank or NBFC, "
    "borrowing is the raw material of the business and its balance sheet has no "
    "current/non-current split, so these ratios don't measure what they measure "
    "for other companies. Lender metrics that matter (net interest margin, gross "
    "NPA, CASA ratio) come from the company's results filings; only an NII-to-assets "
    "proxy for NIM is available from this data source."
)


def sector_profile(info: dict[str, Any] | None) -> str:
    """'lender' for banks, NBFCs and housing-finance companies, else 'general'."""
    info = info or {}
    industry = str(info.get("industry") or "").strip().lower()
    if industry in _LENDER_INDUSTRIES:
        return LENDER
    sector = str(info.get("sector") or "").strip().lower()
    if sector == "financial services" and any(w in industry for w in _LENDER_WORDS):
        return LENDER
    return GENERAL


def suppressed(info: dict[str, Any] | None) -> frozenset[str]:
    return SUPPRESSED[sector_profile(info)]
