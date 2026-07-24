"""
utils/screener_files.py
Shared CSV writer for screener results. Extracted from
agents/screener_agent.py::export_csv() (Phase 7.3 of the graph-expansion
plan) so both the legacy ScreenerAgent and the LangGraph CommunicationLayer
node produce identical output from the same code path.
"""
from __future__ import annotations

import csv
import os
import tempfile


def save_screener_csv(results: list[dict]) -> str | None:
    """Write screener results to a temp CSV and return the file path."""
    if not results:
        return None
    path = os.path.join(tempfile.gettempdir(), "aria_screener_results.csv")
    fieldnames = list(results[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    return path
