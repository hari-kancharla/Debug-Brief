"""Helpers for locating and describing generated reports.

These are read-only utilities used by the ``last`` and ``open`` commands. They
never require an active session and never open files themselves.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import List, Optional

from .markdown import normalize_free_text
from .reporters import VALID_MODES
from .utils import is_regular_file, open_regular_text


def list_reports(reports_dir: Path) -> List[Path]:
    """Return generated report files (Markdown, JSON, and HTML), newest first.

    Only verified regular files are returned: is_regular_file (lstat) is the
    security check, since Path.is_file follows symlinks. A symlinked or special
    report entry is skipped so it is never selected, read, or opened.
    """
    if not reports_dir.is_dir():
        return []
    reports: List[tuple[float, str, Path]] = []
    try:
        entries = reports_dir.iterdir()
        for path in entries:
            if not is_regular_file(path) or path.suffix not in (".md", ".json", ".html"):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            reports.append((mtime, path.name, path))
    except OSError:
        return []
    reports.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in reports]


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


def _plain_title(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"\\([\\`*_{}\[\]|~#>+\-.])", r"\1", text)
    return normalize_free_text(text)


def first_title(report_path: Path) -> Optional[str]:
    """Return a useful plain title from a verified report file."""
    if report_path.suffix == ".json":
        import json

        try:
            with open_regular_text(report_path) as handle:
                data = json.load(handle)
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
            return None
        title = data.get("title") if isinstance(data, dict) else None
        return _plain_title(title) if isinstance(title, str) else None
    if report_path.suffix == ".html":
        try:
            with open_regular_text(report_path) as handle:
                text = handle.read()
        except (OSError, UnicodeError):
            return None
        match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.IGNORECASE | re.DOTALL)
        if match:
            return _plain_title(re.sub(r"<[^>]+>", "", match.group(1)))
        return None
    try:
        with open_regular_text(report_path) as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("# "):
                    return _plain_title(stripped[2:].strip())
                if stripped.startswith("#"):
                    return _plain_title(stripped.lstrip("#").strip())
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return None
