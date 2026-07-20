"""utils/pdf_export.py — Export reports to markdown files (PDF via pandoc if available)"""
import os
from config import REPORTS_DIR
from utils.helpers import now_ist

def save_report(symbol: str, report_type: str, content: str) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    ts    = now_ist().replace(" ","_").replace(",","").replace(":","")[:15]
    fname = os.path.join(REPORTS_DIR, f"{symbol}_{report_type}_{ts}.md")
    with open(fname, "w", encoding="utf-8") as f:
        f.write(content)
    return fname
