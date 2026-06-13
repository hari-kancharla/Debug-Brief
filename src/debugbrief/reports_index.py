"""Helpers for locating and describing generated reports.

These are read-only utilities used by the ``last`` and ``open`` commands. They
never require an active session and never open files themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .reporters import VALID_MODES


def list_reports(reports_dir: Path) -> List[Path]:
    """Return generated report files (Markdown and JSON), newest first."""
    if not reports_dir.is_dir():
        return []
    reports = [
        p
        for p in reports_dir.iterdir()
        if p.is_file() and p.suffix in (".md", ".json")
    ]
    reports.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return reports


def latest_report(reports_dir: Path) -> Optional[Path]:
    reports = list_reports(reports_dir)
    return reports[0] if reports else None


def infer_mode(report_path: Path) -> Optional[str]:
    """Infer the report mode from a filename like ``<id>-pr.md``."""
    stem = report_path.stem  # filename without .md
    for mode in VALID_MODES:
        if stem.endswith(f"-{mode}"):
            return mode
    return None


def first_title(report_path: Path) -> Optional[str]:
    """Return the report title (a markdown H1, or a JSON report's ``title``)."""
    if report_path.suffix == ".json":
        import json

        try:
            with open(report_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        title = data.get("title") if isinstance(data, dict) else None
        return title if isinstance(title, str) else None
    try:
        with open(report_path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("# "):
                    return stripped[2:].strip()
                if stripped.startswith("#"):
                    return stripped.lstrip("#").strip()
    except OSError:
        return None
    return None
