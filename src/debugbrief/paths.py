"""Project-root detection and the on-disk storage layout for DebugBrief.

All state lives under ``<project_root>/.debugbrief/``:

    .debugbrief/
        active_session.json
        sessions/<session_id>.json
        reports/<session_id>-<mode>.md

The project root is the enclosing Git repo root when inside a repo, otherwise
the current working directory.
"""

from __future__ import annotations

import contextlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import git_utils
from .utils import is_regular_file

DEBUGBRIEF_DIRNAME = ".debugbrief"
ACTIVE_SESSION_FILENAME = "active_session.json"
ACTIVE_COMMAND_FILENAME = "active_command.json"
COMMAND_LOCK_FILENAME = ".command.lock"
SESSIONS_DIRNAME = "sessions"
REPORTS_DIRNAME = "reports"


class UnsafeStateDirectory(Exception):
    """Raised when a ``.debugbrief`` state path is a symlink or not a directory.

    DebugBrief refuses to follow a symlinked state path so it cannot be tricked
    into reading or writing session data and reports outside the project.
    """


def is_valid_session_id(session_id: object) -> bool:
    """True for a UUID-shaped id, so a pointer or lease cannot escape ``sessions/``.

    Session ids are uuid4 (hex and dashes). Restricting to those characters means
    a corrupt or hostile pointer/lease can never build a path-traversing session
    path. Used everywhere a session id from disk becomes a file path.
    """
    return (
        isinstance(session_id, str)
        and 0 < len(session_id) <= 64
        and all(c in "0123456789abcdefABCDEF-" for c in session_id)
    )


def _require_real_dir(path: Path, label: str) -> None:
    """Reject an existing state path that is a symlink or not a real directory.

    Uses ``lstat`` so a symlink is detected rather than followed. A path that does
    not exist yet is fine (it will be created as a real directory).
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        return  # cannot stat; a concrete read/write error will surface later
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeStateDirectory(
            f"{label} ({path}) is a symlink; DebugBrief refuses to follow it. "
            "Remove it or replace it with a real directory."
        )
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeStateDirectory(
            f"{label} ({path}) exists but is not a directory. Remove it so "
            "DebugBrief can manage its state there."
        )


@dataclass
class ProjectPaths:
    """Resolved storage locations for a given project root."""

    project_root: Path
    is_git_repo: bool
    repo_root: Optional[Path] = None

    @property
    def base_dir(self) -> Path:
        return self.project_root / DEBUGBRIEF_DIRNAME

    @property
    def active_session_file(self) -> Path:
        return self.base_dir / ACTIVE_SESSION_FILENAME

    @property
    def active_command_file(self) -> Path:
        """Readable metadata for a command captured right now (the lease)."""
        return self.base_dir / ACTIVE_COMMAND_FILENAME

    @property
    def command_lock_file(self) -> Path:
        """Lock held for a command's whole lifetime; auto-released on crash."""
        return self.base_dir / COMMAND_LOCK_FILENAME

    @property
    def sessions_dir(self) -> Path:
        return self.base_dir / SESSIONS_DIRNAME

    @property
    def reports_dir(self) -> Path:
        return self.base_dir / REPORTS_DIRNAME

    def session_file(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    def report_file(self, session_id: str, mode: str) -> Path:
        return self.reports_dir / f"{session_id}-{mode}.md"

    def report_json_file(self, session_id: str, mode: str) -> Path:
        return self.reports_dir / f"{session_id}-{mode}.json"

    def assert_state_dirs_safe(self) -> None:
        """Fail if any existing state directory is a symlink or not a directory.

        Call before creating, reading, or writing under ``.debugbrief`` so a
        planted symlink can never redirect DebugBrief's state elsewhere.
        """
        _require_real_dir(self.base_dir, ".debugbrief")
        _require_real_dir(self.sessions_dir, ".debugbrief/sessions")
        _require_real_dir(self.reports_dir, ".debugbrief/reports")

    def ensure_directories(self) -> None:
        """Create the .debugbrief directory tree, restricted to the owner.

        Stored state can include captured command output and notes, so the tree
        is forced to mode 0700 regardless of the user's umask, so other local
        accounts cannot read a project's debugging history.
        """
        self.assert_state_dirs_safe()
        for directory in (self.base_dir, self.sessions_dir, self.reports_dir):
            directory.mkdir(parents=True, exist_ok=True)
            # Force 0700 regardless of umask; best effort on exotic filesystems.
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o700)


def resolve_project_paths(start: Optional[Path] = None) -> ProjectPaths:
    """Resolve the project root and storage layout from ``start`` (or cwd).

    If inside a Git repo, the repo root is used as the project root. Otherwise
    the current working directory is used and we continue safely.
    """
    cwd = Path(start).resolve() if start is not None else Path.cwd().resolve()
    repo_root = git_utils.find_repo_root(cwd)
    if repo_root is not None:
        root_path = Path(repo_root).resolve()
        return ProjectPaths(
            project_root=root_path, is_git_repo=True, repo_root=root_path
        )
    return ProjectPaths(project_root=cwd, is_git_repo=False, repo_root=None)


def ensure_local_ignore(paths: ProjectPaths) -> Tuple[bool, List[str]]:
    """Ensure ``.debugbrief/`` is ignored locally via ``.git/info/exclude``.

    We never touch a shared/tracked ``.gitignore`` by default; ``.git/info/exclude``
    is local to the clone and not committed. Returns (changed, warnings) where
    ``changed`` indicates whether we wrote a new entry.

    Safe and non-fatal: when not in a repo, or the exclude file is unavailable,
    we return a warning instead of raising.
    """
    warnings: List[str] = []
    if not paths.is_git_repo or paths.repo_root is None:
        return False, warnings

    git_dir = paths.repo_root / ".git"
    # Handle worktrees / submodules where .git is a file pointing elsewhere.
    if git_dir.is_file():
        warnings.append(
            "Could not update .git/info/exclude (this looks like a git worktree "
            "or submodule). .debugbrief/ may not be ignored automatically; add it "
            "to your ignore rules manually if desired."
        )
        return False, warnings

    info_dir = git_dir / "info"
    exclude_file = info_dir / "exclude"
    entry = f"{DEBUGBRIEF_DIRNAME}/"

    # Refuse a symlinked or special exclude file: reading it could block on a
    # FIFO and appending could write through a symlink to an external file. Use
    # lexists, not exists: a dangling symlink reports exists() False, but the
    # later write_text would still follow it and create a file outside the repo,
    # so the entry must be checked regardless of whether its target exists.
    if os.path.lexists(exclude_file) and not is_regular_file(exclude_file):
        warnings.append(
            "Could not update .git/info/exclude (it is not a regular file). "
            ".debugbrief/ may not be ignored automatically; add it to your ignore "
            "rules manually if desired."
        )
        return False, warnings

    try:
        if exclude_file.exists():
            existing = exclude_file.read_text(encoding="utf-8")
            existing_lines = {line.strip() for line in existing.splitlines()}
            if entry in existing_lines or DEBUGBRIEF_DIRNAME in existing_lines:
                return False, warnings
            prefix = "" if existing.endswith("\n") or existing == "" else "\n"
            with open(exclude_file, "a", encoding="utf-8") as handle:
                handle.write(f"{prefix}{entry}\n")
            return True, warnings
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_file.write_text(f"{entry}\n", encoding="utf-8")
        return True, warnings
    except OSError as exc:
        warnings.append(
            f"Could not update .git/info/exclude ({exc}). .debugbrief/ may not be "
            "ignored automatically; add it to your ignore rules manually if desired."
        )
        return False, warnings
