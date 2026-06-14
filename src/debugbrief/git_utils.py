"""Thin, honest wrappers around the native ``git`` executable.

We deliberately shell out to ``git`` via subprocess rather than depend on a
library like GitPython. Every function fails safely: if ``git`` is missing, the
directory is not a repo, or a command errors, callers get conservative defaults
(e.g. ``None`` / empty lists) instead of exceptions.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import GitState

_GIT_TIMEOUT_SECONDS = 15

# Generated/cache artifact names that should never appear in a change summary.
_ARTIFACT_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
        ".tox",
        ".nox",
        ".hypothesis",
        "node_modules",
        ".DS_Store",
    }
)


def _is_generated_artifact(path: str) -> bool:
    """Return True for compiled/cache files that are not meaningful changes.

    Filters out byte-compiled files (``*.pyc``/``*.pyo``), packaging metadata
    (``*.egg-info``), editor/OS cruft (``.DS_Store``), and known cache or vendor
    directories (``__pycache__``, the various ``.*_cache`` trees, ``node_modules``),
    matching on any path segment. Real source files keep their names, so paths
    like ``keymap.py`` or ``api_client.py`` are never filtered.
    """
    if path.endswith((".pyc", ".pyo")):
        return True
    for segment in path.replace("\\", "/").split("/"):
        if not segment:
            continue
        if segment in _ARTIFACT_DIR_NAMES:
            return True
        if segment.endswith(".egg-info"):
            return True
    return False


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
            # Git speaks UTF-8 for paths and refs; decode as UTF-8 regardless of
            # the process locale (a C/POSIX locale would otherwise mangle or fail
            # on non-ASCII filenames), and never crash on an undecodable byte.
            encoding="utf-8",
            errors="replace",
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


def current_short_sha(cwd: Path) -> Optional[str]:
    """Return the abbreviated HEAD SHA, or None when unavailable."""
    ok, out, _ = _run_git(["rev-parse", "--short", "HEAD"], cwd)
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
    return current_sha(cwd) is not None


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
    Combines staged, unstaged, and untracked changes so the result reflects the
    working tree at call time. Safe outside a repo: returns an empty list.

    Uses the NUL-delimited ``-z`` form of ``git status --porcelain`` so paths are
    emitted verbatim. The default form C-quotes and octal-escapes any path with
    non-ASCII bytes (a filename like ``café.py`` would otherwise show up as
    ``caf\\303\\251.py`` in a report).
    """
    ok, out, _ = _run_git(["status", "--porcelain", "-z"], cwd)
    if not ok:
        return []
    seen: Dict[str, str] = {}
    # Entries are separated by NUL. A rename or copy is followed by its origin
    # path as a separate NUL field, which we skip.
    tokens = out.split("\x00")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        if len(entry) < 3:
            i += 1
            continue
        code = entry[:2]  # "XY"; index 2 is a space, the path starts at 3
        path = entry[3:]
        if code[:1] in ("R", "C"):
            i += 2  # consume this entry and its origin-path field
        else:
            i += 1
        if not path or _is_generated_artifact(path):
            continue
        label = _porcelain_label(code)
        # If a path appears twice, keep the first (most significant) label.
        seen.setdefault(path, label)
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


def _file_fingerprint(cwd: Path, path: str, deleted: bool) -> str:
    """A content fingerprint for a working-tree path (sentinel if not a file).

    Uses ``lstat`` to inspect the path before opening it, so a session never
    blocks or follows a link to capture a baseline:

    - regular file: hashed in fixed-size chunks (bounded memory on a huge file);
    - symlink: the link target string is hashed, never followed (the target may
      be outside the repo, missing, or itself a blocking special file);
    - FIFO, socket, or device: a stable type sentinel, never opened (opening a
      FIFO would block waiting for a writer);
    - unreadable regular file: a sentinel from safe stat metadata, so a later
      change is still noticed without raising.
    """
    if deleted:
        return "<deleted>"
    full = Path(cwd) / path
    try:
        info = os.lstat(full)
    except OSError:
        return "<unreadable>"

    mode = info.st_mode
    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(full)
        except OSError:
            return "<unreadable-symlink>"
        return "symlink:" + hashlib.sha256(os.fsencode(target)).hexdigest()
    if not stat.S_ISREG(mode):
        # FIFO/socket/device/other: identify by type without opening it.
        return f"<special:{stat.S_IFMT(mode)}>"

    digest = hashlib.sha256()
    try:
        with open(full, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return f"<unreadable:{info.st_size}:{int(info.st_mtime)}>"
    return digest.hexdigest()


def working_tree_fingerprints(cwd: Path) -> Dict[str, str]:
    """Fingerprint every currently-changed file (modified, deleted, untracked).

    Captured at session start as a baseline so the final report can tell which
    of those files were actually changed during the session.
    """
    return {
        path: _file_fingerprint(cwd, path, deleted=(label == "D"))
        for label, path in name_status(cwd)
    }


def _parse_diff_name_status(out: str) -> List[Tuple[str, str]]:
    """Parse ``git diff --name-status -z`` into (label, path) pairs."""
    tokens = out.split("\x00")
    pairs: List[Tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        code = tokens[i]
        if not code:
            i += 1
            continue
        status = code[:1]
        if status in ("R", "C") and i + 2 < len(tokens):
            new_path = tokens[i + 2]  # STATUS \0 old \0 new
            i += 3
            if new_path and not _is_generated_artifact(new_path):
                pairs.append(("R" if status == "R" else "A", new_path))
            continue
        if i + 1 < len(tokens):
            path = tokens[i + 1]
            i += 2
            if path and not _is_generated_artifact(path):
                pairs.append((_porcelain_label(status + " "), path))
        else:
            break
    return pairs


def _untracked_files(cwd: Path) -> List[str]:
    ok, out, _ = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd)
    if not ok:
        return []
    return [p for p in out.split("\x00") if p and not _is_generated_artifact(p)]


def session_changes(
    cwd: Path, initial_sha: Optional[str], baseline: Dict[str, str]
) -> Tuple[List[Tuple[str, str]], int, int]:
    """Files changed during the session, as ``(pairs, added, deleted)``.

    A file counts when it differs from the starting commit (committed or
    uncommitted) or is newly untracked, and its current content differs from its
    state at session start, so a file left dirty from before the session and
    untouched since is excluded. Lines are measured against the starting commit.
    Falls back to the plain working-tree diff when there is no starting commit.
    """
    if not initial_sha:
        return name_status(cwd), *shortstat(cwd)

    candidates: Dict[str, str] = {}
    ok, out, _ = _run_git(["diff", "--name-status", "-z", initial_sha], cwd)
    if ok:
        for label, path in _parse_diff_name_status(out):
            candidates.setdefault(path, label)
    for path in _untracked_files(cwd):
        candidates.setdefault(path, "A")

    pairs: List[Tuple[str, str]] = []
    for path, label in candidates.items():
        current = _file_fingerprint(cwd, path, deleted=(label == "D"))
        if baseline.get(path) == current:
            continue  # already in this exact state before the session started
        pairs.append((label, path))
    pairs.sort(key=lambda item: item[1])

    added, deleted = 0, 0
    ok, out, _ = _run_git(["diff", "--shortstat", initial_sha], cwd)
    if ok and out.strip():
        added, deleted = _parse_shortstat(out)
    return pairs, added, deleted


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
