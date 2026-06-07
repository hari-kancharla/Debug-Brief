"""Thin, honest wrappers around the native ``git`` executable.

We deliberately shell out to ``git`` via subprocess rather than depend on a
library like GitPython. Every function fails safely: if ``git`` is missing, the
directory is not a repo, or a command errors, callers get conservative defaults
(e.g. ``None`` / empty lists) instead of exceptions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .models import GitState

_GIT_TIMEOUT_SECONDS = 15


def _run_git(args: List[str], cwd: Path) -> Tuple[bool, str, str]:
    """Run ``git <args>`` in ``cwd``.

    Returns (success, stdout, stderr). ``success`` is True only when git is
    available and exits 0.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        # git is not installed / not on PATH.
        return False, "", "git executable not found"
    except subprocess.TimeoutExpired:
        return False, "", "git command timed out"
    except OSError as exc:  # pragma: no cover - defensive
        return False, "", str(exc)
    return completed.returncode == 0, completed.stdout, completed.stderr


def is_git_available() -> bool:
    """Return True if a ``git`` executable can be invoked."""
    ok, _, _ = _run_git(["--version"], Path.cwd())
    return ok


def find_repo_root(cwd: Path) -> Optional[str]:
    """Return the absolute path of the enclosing Git repo root, or None."""
    ok, out, _ = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if not ok:
        return None
    root = out.strip()
    return root or None


def is_inside_repo(cwd: Path) -> bool:
    ok, out, _ = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return ok and out.strip() == "true"


def current_sha(cwd: Path) -> Optional[str]:
    """Return the full HEAD SHA, or None (e.g. a repo with no commits yet)."""
    ok, out, _ = _run_git(["rev-parse", "HEAD"], cwd)
    if not ok:
        return None
    sha = out.strip()
    return sha or None


def is_detached_head(cwd: Path) -> bool:
    """Return True if HEAD is detached.

    ``git symbolic-ref -q HEAD`` exits nonzero when HEAD is detached.
    """
    ok, out, _ = _run_git(["symbolic-ref", "-q", "HEAD"], cwd)
    if ok and out.strip():
        return False
    # If there are no commits yet, treat HEAD as not detached (branch exists).
    if current_sha(cwd) is None:
        return False
    return True


def current_branch(cwd: Path) -> Optional[str]:
    """Return the current branch name, or None when detached / unborn."""
    ok, out, _ = _run_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd)
    if not ok:
        return None
    branch = out.strip()
    return branch or None


def _porcelain_label(code: str) -> str:
    """Map a 2-char porcelain status code to a single M/A/D/R-style label.

    Untracked files (``??``) are reported as ``A`` (added/new). For other
    entries we use the index status when present, otherwise the worktree status.
    """
    if code == "??":
        return "A"
    x = code[0] if len(code) >= 1 else " "
    y = code[1] if len(code) >= 2 else " "
    primary = x if x != " " else y
    mapping = {
        "M": "M",  # modified
        "A": "A",  # added
        "D": "D",  # deleted
        "R": "R",  # renamed
        "C": "A",  # copied -> treat as added
        "T": "M",  # type change -> treat as modified
        "U": "M",  # unmerged -> treat as modified
    }
    return mapping.get(primary, primary if primary.strip() else "M")


def name_status(cwd: Path) -> List[Tuple[str, str]]:
    """Return sorted (label, path) pairs describing changed files.

    Labels are M (modified), A (added/new), D (deleted), or R (renamed).
    Combines staged, unstaged, and untracked changes via ``git status
    --porcelain`` so the result reflects the working tree at call time. Safe
    outside a repo: returns an empty list.
    """
    ok, out, _ = _run_git(["status", "--porcelain"], cwd)
    if not ok:
        return []
    seen = {}
    for line in out.splitlines():
        if not line.strip() or len(line) < 3:
            continue
        # Porcelain format: "XY <path>" or "XY <old> -> <new>" for renames.
        code = line[:2]
        path_part = line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        path_part = path_part.strip().strip('"')
        if not path_part:
            continue
        label = _porcelain_label(code)
        # If a path appears twice, keep the first (most significant) label.
        seen.setdefault(path_part, label)
    return sorted(
        ((label, path) for path, label in seen.items()), key=lambda item: item[1]
    )


def changed_files(cwd: Path) -> List[str]:
    """Return a sorted, de-duplicated list of changed files.

    Combines tracked changes (staged + unstaged) and untracked files from
    ``git status --porcelain`` so the result reflects the working-tree state.
    """
    return [path for _label, path in name_status(cwd)]


def shortstat(cwd: Path) -> Tuple[int, int]:
    """Return (lines_added, lines_deleted) for unstaged + staged changes.

    Uses ``git diff HEAD --shortstat`` which compares the working tree against
    HEAD. Returns (0, 0) when there are no changes or stats are unavailable.
    """
    ok, out, _ = _run_git(["diff", "HEAD", "--shortstat"], cwd)
    if not ok or not out.strip():
        return 0, 0
    return _parse_shortstat(out)


def _parse_shortstat(text: str) -> Tuple[int, int]:
    """Parse a ``--shortstat`` summary line into (added, deleted).

    Example input:
        " 3 files changed, 12 insertions(+), 4 deletions(-)"
    """
    added = 0
    deleted = 0
    for chunk in text.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if "insertion" in chunk:
            added = _leading_int(chunk)
        elif "deletion" in chunk:
            deleted = _leading_int(chunk)
    return added, deleted


def _leading_int(text: str) -> int:
    digits = ""
    for ch in text.strip():
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else 0


def capture_state(cwd: Path, initial: bool = True) -> GitState:
    """Capture a snapshot of the Git state for ``cwd``.

    Safe outside a repo: returns ``GitState(is_repo=False)``.
    """
    repo_root = find_repo_root(cwd)
    if repo_root is None:
        return GitState(is_repo=False)

    detached = is_detached_head(cwd)
    sha = current_sha(cwd)
    branch = None if detached else current_branch(cwd)

    state = GitState(
        is_repo=True,
        repo_root=repo_root,
        branch=branch,
        detached_head=detached,
    )
    if initial:
        state.initial_sha = sha
    else:
        state.final_sha = sha
    return state
