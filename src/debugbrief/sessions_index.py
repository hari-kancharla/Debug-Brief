"""Read-only helpers for enumerating and resolving stored sessions.

Used by the ``list`` and ``show`` commands. None of these require an active
session, and they never mutate state.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .models import Session
from .paths import ProjectPaths
from .reporters import VALID_MODES, build_context
from .utils import parse_iso8601, read_json


def _start_seconds(session: Session) -> float:
    start = session.timestamps.start
    if not start:
        return 0.0
    try:
        return parse_iso8601(start).timestamp()
    except (ValueError, TypeError):
        return 0.0


def load_all_sessions(paths: ProjectPaths) -> List[Session]:
    """Load every stored session, most recent first (by start time).

    Unreadable session files are skipped silently so a single corrupt file
    cannot break listing.
    """
    paths.assert_state_dirs_safe()  # refuse a symlinked .debugbrief on read too
    sessions_dir = paths.sessions_dir
    if not sessions_dir.is_dir():
        return []
    sessions: List[Session] = []
    for path in sessions_dir.glob("*.json"):
        if not path.is_file():
            continue
        try:
            sessions.append(Session.from_dict(read_json(path)))
        except (ValueError, OSError, TypeError):
            continue
    sessions.sort(key=lambda s: (_start_seconds(s), s.session_id), reverse=True)
    return sessions


def report_modes_for(paths: ProjectPaths, session_id: str) -> List[str]:
    """Return the report modes that have been generated for ``session_id``."""
    modes = []
    for mode in VALID_MODES:
        if (
            paths.report_file(session_id, mode).exists()
            or paths.report_json_file(session_id, mode).exists()
        ):
            modes.append(mode)
    return modes


def is_verified(session: Session) -> bool:
    """True if at least one verification command passed during the session."""
    return len(build_context(session).verification_commands) > 0


def resolve_session_id(
    paths: ProjectPaths, prefix: str
) -> Tuple[Optional[str], List[str]]:
    """Resolve a (possibly short) session id prefix to a full id.

    Returns (resolved_id, matches). ``resolved_id`` is set only when exactly one
    session id matches; otherwise it is None and ``matches`` lists all candidate
    ids (empty when there is no match, multiple when the prefix is ambiguous).
    """
    clean = prefix.strip()
    ids = [s.session_id for s in load_all_sessions(paths)]
    if clean in ids:
        return clean, [clean]
    matches = [sid for sid in ids if sid.startswith(clean)]
    if len(matches) == 1:
        return matches[0], matches
    return None, matches
