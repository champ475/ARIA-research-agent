"""
tools/data/pdf_scraper.py
Scrapes annual reports and quarterly results PDFs from NSE/BSE.
Extracts text using pdfplumber. Caches in Postgres (pdf_cache table).
"""

import pdfplumber
import io
import logging
from utils import http
from utils.db import pdf_cache_get, pdf_cache_set

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com/",
}


def _pdf_to_text(content: bytes, max_pages: int = 30) -> str:
    """Extract text from PDF bytes using pdfplumber."""
    try:
        text_parts = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for i, page in enumerate(pdf.pages[:max_pages]):
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        return "\n".join(text_parts)
    except Exception as e:
        log.warning(f"PDF extraction failed: {e}")
        return ""


def fetch_nse_filings_list(symbol: str, filing_type: str = "Quarterly Results") -> list[dict]:
    """
    Fetch list of filings from NSE corporate filings API.
    filing_type: 'Quarterly Results' | 'Annual Report' | 'Investor Presentation'
    """
    try:
        s = http.session("nse", headers=HEADERS)
        s.get("https://www.nseindia.com", timeout=10)

        url = (
            f"https://www.nseindia.com/api/corporates-corporateActions"
            f"?index=equities&symbol={symbol}&subject={filing_type.replace(' ', '%20')}"
        )
        r = s.get(url, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        return [
            {
                "date"    : item.get("date", ""),
                "subject" : item.get("subject", ""),
                "company" : item.get("companyName", symbol),
                "attachment": item.get("attachment", ""),
            }
            for item in data[:10]
        ]
    except Exception as e:
        log.warning(f"NSE filings list for {symbol} failed: {e}")
        return []


def fetch_bse_filings_list(symbol: str, bse_code: str = "") -> list[dict]:
    """
    Fetch quarterly results filings from BSE.
    Requires BSE scrip code. Falls back gracefully.
    """
    if not bse_code:
        return []
    try:
        url = (
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnualReport/w"
            f"?scripcode={bse_code}&type=Quarterly"
        )
        r = http.get(url, source="bse", headers=HEADERS, timeout=(3, 15))
        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("Table", []) or []
        return [
            {
                "date"    : item.get("SubmissionDate", ""),
                "subject" : item.get("NewsType", ""),
                "url"     : f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{item.get('AttachmentName', '')}",
            }
            for item in items[:10]
        ]
    except Exception as e:
        log.warning(f"BSE filings for {symbol} failed: {e}")
        return []


def fetch_pdf_text(symbol: str, url: str, doc_type: str, period: str = None) -> str:
    """
    Download and extract text from a filing PDF.
    Checks SQLite cache first.
    """
    # Check cache
    cached = pdf_cache_get(symbol, doc_type, period)
    if cached:
        return cached

    if not url:
        return ""

    try:
        s = http.session("nse", headers=HEADERS)
        s.get("https://www.nseindia.com", timeout=10)

        r = s.get(url, timeout=30)
        if r.status_code != 200 or len(r.content) < 500:
            return ""

        content_type = r.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not url.endswith(".pdf"):
            return ""

        text = _pdf_to_text(r.content, max_pages=40)
        if text:
            pdf_cache_set(symbol, doc_type, text, period=period, url=url)
        return text

    except Exception as e:
        log.warning(f"PDF fetch for {symbol} from {url} failed: {e}")
        return ""


def get_latest_quarterly_results(symbol: str) -> str:
    """
    High-level: get the most recent quarterly results PDF text.
    Returns a summary-ready string.
    """
    cached = pdf_cache_get(symbol, "quarterly_results")
    if cached:
        return cached[:8000]  # Return up to 8K chars

    filings = fetch_nse_filings_list(symbol, "Quarterly Results")
    if not filings:
        return f"No quarterly results filings found on NSE for {symbol}."

    latest  = filings[0]
    url     = latest.get("attachment", "")
    period  = latest.get("date", "")

    if not url:
        return f"Quarterly results filing found ({period}) but no attachment URL available."

    if not url.startswith("http"):
        url = f"https://nsearchives.nseindia.com/corporate/xbrl/{url}"

    text = fetch_pdf_text(symbol, url, "quarterly_results", period)
    if not text:
        return f"Could not extract text from quarterly results PDF for {symbol}."

    return text[:8000]


def get_annual_report_text(symbol: str) -> str:
    """
    High-level: get the most recent annual report PDF text.
    """
    cached = pdf_cache_get(symbol, "annual_report")
    if cached:
        return cached[:10000]

    filings = fetch_nse_filings_list(symbol, "Annual Report")
    if not filings:
        return f"No annual report found on NSE for {symbol}."

    latest = filings[0]
    url    = latest.get("attachment", "")
    period = latest.get("date", "")

    if not url:
        return f"Annual report filing found ({period}) but no PDF attachment available."

    text = fetch_pdf_text(symbol, url, "annual_report", period)
    if not text:
        return f"Could not extract annual report text for {symbol}."

    return text[:10000]
