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

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import git_utils

DEBUGBRIEF_DIRNAME = ".debugbrief"
ACTIVE_SESSION_FILENAME = "active_session.json"
SESSIONS_DIRNAME = "sessions"
REPORTS_DIRNAME = "reports"


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
    def sessions_dir(self) -> Path:
        return self.base_dir / SESSIONS_DIRNAME

    @property
    def reports_dir(self) -> Path:
        return self.base_dir / REPORTS_DIRNAME

    def session_file(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    def report_file(self, session_id: str, mode: str) -> Path:
        return self.reports_dir / f"{session_id}-{mode}.md"

    def ensure_directories(self) -> None:
        """Create the .debugbrief directory tree if it does not exist."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)


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
        else:
            info_dir.mkdir(parents=True, exist_ok=True)
            exclude_file.write_text(f"{entry}\n", encoding="utf-8")
            return True, warnings
    except OSError as exc:
        warnings.append(
            f"Could not update .git/info/exclude ({exc}). .debugbrief/ may not be "
            "ignored automatically; add it to your ignore rules manually if desired."
        )
        return False, warnings
