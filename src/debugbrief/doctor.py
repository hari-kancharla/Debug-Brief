"""Health-check logic for ``debugbrief doctor``.

Runs a series of read-only checks (with an optional, safe ``--fix``) and reports
PASS / WARN / FAIL lines plus an overall verdict and exit code:

    0  ready (all PASS)
    1  usable with warnings (>=1 WARN, no FAIL)
    2  blocking issues (>=1 FAIL)
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import config, git_utils
from .paths import (
    DEBUGBRIEF_DIRNAME,
    ProjectPaths,
    UnsafeStateDirectory,
    ensure_local_ignore,
    is_valid_session_id,
)
from .utils import is_regular_file, is_supported_platform, read_json_safe

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

EXIT_READY = 0
EXIT_WARN = 1
EXIT_BLOCKED = 2


@dataclass
class CheckResult:
    level: str
    name: str
    detail: str = ""


@dataclass
class DoctorReport:
    checks: List[CheckResult]
    exit_code: int
    summary: str


def _overall(checks: List[CheckResult]) -> Tuple[int, str]:
    if any(c.level == FAIL for c in checks):
        return EXIT_BLOCKED, "DebugBrief has blocking issues."
    if any(c.level == WARN for c in checks):
        return EXIT_WARN, "DebugBrief is usable with warnings."
    return EXIT_READY, "DebugBrief is ready."


def _exclude_has_entry(paths: ProjectPaths) -> Optional[bool]:
    """Return True/False if the exclude entry is present, or None if N/A."""
    if not paths.is_git_repo or paths.repo_root is None:
        return None
    exclude = paths.repo_root / ".git" / "info" / "exclude"
    if not exclude.exists():
        return False
    if not is_regular_file(exclude):
        return False  # do not follow a symlink or block on a FIFO here
    try:
        lines = {
            line.strip() for line in exclude.read_text(encoding="utf-8").splitlines()
        }
    except OSError:
        return False
    return f"{DEBUGBRIEF_DIRNAME}/" in lines or DEBUGBRIEF_DIRNAME in lines


def run_doctor(paths: ProjectPaths, fix: bool = False) -> DoctorReport:
    checks: List[CheckResult] = []

    # Reject a symlinked or non-directory state path up-front. If unsafe, report
    # the failure and stop: the remaining checks read .debugbrief, and continuing
    # would follow the very symlink we are refusing.
    try:
        paths.assert_state_dirs_safe()
    except UnsafeStateDirectory as exc:
        checks.append(CheckResult(FAIL, "State directory", str(exc)))
        checks.append(
            CheckResult(
                FAIL,
                "Remaining checks",
                "skipped; refusing to read through an unsafe .debugbrief path.",
            )
        )
        exit_code, summary = _overall(checks)
        return DoctorReport(checks=checks, exit_code=exit_code, summary=summary)

    # Optional safe fixes applied up-front so subsequent checks reflect them.
    fix_notes: List[str] = []
    if fix:
        try:
            paths.ensure_directories()
            fix_notes.append("ensured .debugbrief/ directories exist")
        except OSError as exc:
            fix_notes.append(f"could not create .debugbrief/ ({exc})")
        changed, _warnings = ensure_local_ignore(paths)
        if changed:
            fix_notes.append("added .debugbrief/ to .git/info/exclude")

    # 1. Platform
    if is_supported_platform():
        checks.append(CheckResult(PASS, "Platform", f"{platform.system()} (supported)"))
    else:
        checks.append(
            CheckResult(
                FAIL,
                "Platform",
                f"{platform.system()} is not supported (Unix-like only).",
            )
        )

    # 2. Python version
    py = ".".join(str(v) for v in sys.version_info[:3])
    if sys.version_info[:2] >= (3, 9):
        checks.append(CheckResult(PASS, "Python version", f"{py} (>= 3.9)"))
    else:
        checks.append(
            CheckResult(FAIL, "Python version", f"{py} (3.9+ required)")
        )

    # 3. Project root
    checks.append(
        CheckResult(PASS, "Project root", str(paths.project_root))
    )

    # 4. Inside a Git repo?
    if paths.is_git_repo:
        checks.append(CheckResult(PASS, "Git repository", "inside a Git repo"))
        # 5. Branch / detached HEAD
        if git_utils.is_detached_head(paths.project_root):
            checks.append(
                CheckResult(
                    WARN,
                    "Git branch",
                    "HEAD is detached; consider working on a branch.",
                )
            )
        else:
            branch = git_utils.current_branch(paths.project_root) or "(unborn branch)"
            checks.append(CheckResult(PASS, "Git branch", branch))
    else:
        checks.append(
            CheckResult(
                WARN,
                "Git repository",
                "not inside a Git repo; Git metadata capture is disabled "
                "(this is supported).",
            )
        )

    # 6. .debugbrief directory exists?
    if paths.base_dir.is_dir():
        checks.append(
            CheckResult(PASS, ".debugbrief directory", str(paths.base_dir))
        )
    else:
        checks.append(
            CheckResult(
                WARN,
                ".debugbrief directory",
                "does not exist yet (created on first 'start', or run "
                "'debugbrief doctor --fix').",
            )
        )

    # 7. Writable / creatable?
    writable_target = paths.base_dir if paths.base_dir.exists() else paths.project_root
    if os.access(writable_target, os.W_OK):
        checks.append(
            CheckResult(PASS, "Storage writable", f"{writable_target} is writable")
        )
    else:
        checks.append(
            CheckResult(
                FAIL,
                "Storage writable",
                f"{writable_target} is not writable; cannot persist sessions.",
            )
        )

    # 8. .git/info/exclude contains .debugbrief/
    has_exclude = _exclude_has_entry(paths)
    if has_exclude is None:
        checks.append(
            CheckResult(PASS, "Local ignore", "N/A (not a Git repo)")
        )
    elif has_exclude:
        checks.append(
            CheckResult(PASS, "Local ignore", ".debugbrief/ is in .git/info/exclude")
        )
    else:
        checks.append(
            CheckResult(
                WARN,
                "Local ignore",
                ".debugbrief/ is not in .git/info/exclude (run "
                "'debugbrief doctor --fix' to add it).",
            )
        )

    # 9-12. Active session checks
    _active_session_checks(paths, checks)

    # 13. Reports directory writable
    reports_dir = paths.reports_dir
    if reports_dir.is_dir():
        if os.access(reports_dir, os.W_OK):
            checks.append(
                CheckResult(PASS, "Reports directory", f"{reports_dir} (writable)")
            )
        else:
            checks.append(
                CheckResult(
                    FAIL, "Reports directory", f"{reports_dir} is not writable."
                )
            )
    else:
        checks.append(
            CheckResult(
                WARN,
                "Reports directory",
                "does not exist yet (created on 'end', or run "
                "'debugbrief doctor --fix').",
            )
        )

    # 14. Optional project config (.debugbrief.toml)
    cfg_error = config.parse_error(paths.project_root)
    if cfg_error is None:
        checks.append(
            CheckResult(PASS, "Project config", ".debugbrief.toml is valid or absent")
        )
    else:
        checks.append(
            CheckResult(
                WARN,
                "Project config",
                f"{cfg_error}; its defaults are not applied. Fix the TOML to use it.",
            )
        )

    # 15. Experimental shell mode
    checks.append(
        CheckResult(
            PASS,
            "Experimental shell mode",
            "'start --shell' is unavailable by design in v1.",
        )
    )

    exit_code, summary = _overall(checks)
    if fix and fix_notes:
        # Surface what --fix did as an informational PASS line.
        checks.insert(
            0, CheckResult(PASS, "Applied --fix", "; ".join(fix_notes))
        )
    return DoctorReport(checks=checks, exit_code=exit_code, summary=summary)


def _active_session_checks(
    paths: ProjectPaths, checks: List[CheckResult]
) -> None:
    pointer_path = paths.active_session_file
    if not pointer_path.exists():
        checks.append(
            CheckResult(PASS, "Active session", "none (no active_session.json)")
        )
        return

    # 9. exists and is a regular file (never follow a symlink / block on a FIFO)
    if not is_regular_file(pointer_path):
        checks.append(
            CheckResult(
                FAIL,
                "Active session",
                "active_session.json is not a regular file (symlink or special); "
                "remove it to recover.",
            )
        )
        return
    checks.append(CheckResult(PASS, "Active session", "active_session.json exists"))

    # 10. valid JSON (read through the safe reader)
    try:
        pointer = read_json_safe(pointer_path)
    except (ValueError, OSError) as exc:
        checks.append(
            CheckResult(
                FAIL,
                "Active session JSON",
                f"active_session.json is not valid JSON ({exc}). Remove it to recover.",
            )
        )
        return
    if not isinstance(pointer, dict) or not is_valid_session_id(pointer.get("session_id")):
        # Reject a missing or non-UUID session id, so a traversal value such as
        # "../../outside" can never build a path that escapes sessions/.
        checks.append(
            CheckResult(
                FAIL,
                "Active session JSON",
                "active_session.json is malformed (missing or invalid session_id).",
            )
        )
        return
    checks.append(CheckResult(PASS, "Active session JSON", "valid"))

    session_id = pointer["session_id"]
    session_file = paths.session_file(session_id)

    # 12. interrupted?
    if not session_file.exists():
        checks.append(
            CheckResult(
                WARN,
                "Session integrity",
                "session looks interrupted (session file missing). Run "
                "'debugbrief status' for recovery steps.",
            )
        )
        return
    if not is_regular_file(session_file):
        checks.append(
            CheckResult(
                FAIL,
                "Session integrity",
                "session file is not a regular file (symlink or special).",
            )
        )
        return

    try:
        session_data = read_json_safe(session_file)
    except (ValueError, OSError) as exc:
        checks.append(
            CheckResult(
                FAIL,
                "Session integrity",
                f"session file is unreadable ({exc}).",
            )
        )
        return

    status = session_data.get("status")
    if status != "ACTIVE":
        checks.append(
            CheckResult(
                WARN,
                "Session integrity",
                f"active pointer references a session with status {status!r}.",
            )
        )
    else:
        checks.append(CheckResult(PASS, "Session integrity", "consistent"))

    # 11. points to current project root?
    session_root = session_data.get("project_root", "")
    try:
        same = Path(session_root).resolve() == Path(paths.project_root).resolve()
    except (OSError, ValueError):
        same = session_root == str(paths.project_root)
    if same:
        checks.append(
            CheckResult(PASS, "Session project root", "matches current project")
        )
    else:
        checks.append(
            CheckResult(
                WARN,
                "Session project root",
                f"active session root ({session_root}) differs from current "
                f"project root ({paths.project_root}).",
            )
        )
