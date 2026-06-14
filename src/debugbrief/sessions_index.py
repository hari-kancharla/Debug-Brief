"""Read-only helpers for enumerating and resolving stored sessions.

Used by the ``list`` and ``show`` commands. None of these require an active
session, and they never mutate state.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .models import Session
from .paths import ProjectPaths
from .reporters import VALID_MODES, build_context
from .utils import is_regular_file, parse_iso8601, read_json_safe


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
        # is_regular_file (lstat) is the security check; Path.is_file follows
        # symlinks. A symlinked or special entry is skipped, never followed, so
        # one unsafe file cannot expose an external file or block the others.
        if not is_regular_file(path):
            continue
        try:
            sessions.append(Session.from_dict(read_json_safe(path)))
        except (ValueError, OSError, TypeError):
            continue  # corrupt or unreadable: skip without blocking other sessions
    sessions.sort(key=lambda s: (_start_seconds(s), s.session_id), reverse=True)
    return sessions


def report_modes_for(paths: ProjectPaths, session_id: str) -> List[str]:
    """Return the report modes that have been generated for ``session_id``."""
    modes = []
    for mode in VALID_MODES:
        # is_regular_file, not exists(): a symlinked report must not count.
        if (
            is_regular_file(paths.report_file(session_id, mode))
            or is_regular_file(paths.report_json_file(session_id, mode))
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
