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
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .models import GitState
from .utils import open_regular_binary

_GIT_TIMEOUT_SECONDS = 15

# Environment variables that redirect git's repository, work-tree, or index
# selection. They are stripped only from the redo provenance query (see
# tracked_state) so that query always discovers the repository from cwd: an
# inherited or stale GIT_DIR otherwise "turns off the repository discovery" (per
# git's docs), so rev-parse reports "not a git repository" inside a real worktree
# and the check would treat tracked, repository-supplied state as untracked and
# fail open. Report snapshots deliberately keep these variables so they stay
# aligned with a caller's legitimate env-selected work tree or index.
_GIT_DISCOVERY_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)

# Git environment variables that could make git's view of which paths are tracked
# differ from plain cwd discovery: a different repository, work tree, or index. If
# any is set, the inherited pass is consulted alongside the sanitized one, so a
# session tracked only in the caller's selected repository, work tree, or index is
# still caught. Running the inherited pass is harmless when nothing differs.
# Object-storage variables and the discovery-only limiters (a ceiling, the
# cross-filesystem flag) change neither which paths are tracked nor whether a
# repository exists, so they are excluded.
_GIT_TRACKING_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")


def _git_tracking_env_present() -> bool:
    """True if the environment could change which paths git reports as tracked."""
    return any(os.environ.get(var) for var in _GIT_TRACKING_ENV)


def _repository_plausibly_present(cwd: Path, *, use_env: bool) -> bool:
    """Whether a git repository is plausibly involved, for the fail-closed branch.

    Decides UNKNOWN (a repository we could not read, so redo fails closed) versus
    UNTRACKED (genuinely no repository, so redo is safe) when git could not
    establish a repository from ``cwd``. Keyed on whether a repository actually
    exists, not merely on which environment variables are set, because no single
    setting distinguishes a stale variable in a plain directory from one
    designating a real work tree.

    A ``.git`` at or above ``cwd`` always counts. On the inherited pass
    (``use_env``) a repository the caller's environment designates also counts:
    ``GIT_DIR`` names one outright, ``GIT_WORK_TREE`` designates a work tree that
    itself has a ``.git``, and ``GIT_COMMON_DIR`` points at an existing git common
    directory. A stale variable that points nowhere does not make a plain
    directory look like a repository, so a bare ``GIT_WORK_TREE`` stays untracked
    while one pointing at a real checkout fails closed.
    """
    if _has_dotgit_ancestor(cwd):
        return True
    if not use_env:
        return False
    if os.environ.get("GIT_DIR"):
        return True
    work_tree = os.environ.get("GIT_WORK_TREE")
    if work_tree and _has_dotgit_ancestor(Path(work_tree)):
        return True
    common_dir = os.environ.get("GIT_COMMON_DIR")
    return bool(common_dir and os.path.isdir(common_dir))


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
        ".debugbrief",
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


def _run_git_rc(
    args: List[str],
    cwd: Path,
    *,
    stable_locale: bool = False,
    discover_from_cwd: bool = False,
) -> Tuple[Optional[int], str, str]:
    """Run ``git <args>`` in ``cwd``, returning (returncode, stdout, stderr).

    ``returncode`` is ``None`` when git could not be run at all (not installed,
    timed out, or another OS error), which a caller must treat as an unknown
    result rather than a definitive answer.

    ``stable_locale`` forces a ``C`` locale so git's diagnostics stay English and
    classifiable; both redo provenance passes need it because they recognize an
    untracked path by git's "did not match" message. ``discover_from_cwd``
    additionally strips the repository/work-tree/index selection variables so
    discovery starts from ``cwd`` (it implies a stable locale); only the sanitized
    provenance pass uses it. Every other caller inherits the caller's environment
    unchanged, so a report snapshot stays aligned with a legitimate env-selected
    work tree or index. Path output is requested with ``-z`` elsewhere, so it is
    unaffected by the forced locale.
    """
    env: Optional[Dict[str, str]] = None
    if stable_locale or discover_from_cwd:
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        if discover_from_cwd:
            for var in _GIT_DISCOVERY_ENV:
                env.pop(var, None)
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
            env=env,
        )
    except FileNotFoundError:
        # git is not installed / not on PATH.
        return None, "", "git executable not found"
    except subprocess.TimeoutExpired:
        return None, "", "git command timed out"
    except OSError as exc:  # pragma: no cover - defensive
        return None, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


def _run_git(args: List[str], cwd: Path) -> Tuple[bool, str, str]:
    """Run an internal Git query, retrying stale repository-selection env."""
    rc, out, err = _run_git_rc(args, cwd)
    if rc != 0 and any(
        marker in err.lower()
        for marker in (
            "not a git repository",
            "does not appear to be a git repository",
            "this operation must be run in a work tree",
        )
    ):
        retry_rc, retry_out, _retry_err = _run_git_rc(
            args, cwd, discover_from_cwd=True
        )
        if retry_rc == 0:
            return True, retry_out, err
    return rc == 0, out, err


def is_git_available() -> bool:
    """Return True if a ``git`` executable can be invoked."""
    ok, _, _ = _run_git(["--version"], Path.cwd())
    return ok


def find_repo_root(cwd: Path) -> Optional[str]:
    """Return the absolute path of the enclosing Git repo root, or None."""
    inherited_ok, inherited_out, _ = _run_git(
        ["rev-parse", "--show-toplevel"], cwd
    )
    if inherited_ok:
        root = inherited_out.rstrip("\r\n")
        return root or None
    sanitized_rc, sanitized_out, _ = _run_git_rc(
        ["rev-parse", "--show-toplevel"], cwd, discover_from_cwd=True
    )
    if sanitized_rc != 0:
        return None
    root = sanitized_out.rstrip("\r\n")
    return root or None


def is_inside_repo(cwd: Path) -> bool:
    ok, out, _ = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return ok and out.strip() == "true"


class TrackedState(Enum):
    """Whether a path is committed to Git, as far as we could determine.

    Distinguishing ``UNKNOWN`` from ``UNTRACKED`` is a security requirement: the
    provenance check that guards ``redo`` must fail closed. Collapsing "could not
    check" into "not tracked" would let a repository-supplied command run in
    exactly the cases where git is unavailable, times out, or rejects the
    repository (e.g. ``safe.directory`` ownership) and provenance is unknowable.
    """

    TRACKED = "tracked"
    UNTRACKED = "untracked"
    UNKNOWN = "unknown"


def _is_not_a_repository(stderr: str) -> bool:
    """True if git's error means "this is simply not a Git repository".

    Distinguished from other fatal exit-128 errors (dubious ownership, a corrupt
    repository) which leave provenance unknowable. Relies on the forced ``C``
    locale in :func:`_run_git_rc` to keep this message in English. Note git emits
    this same message for a present-but-broken ``.git`` (e.g. a missing ``HEAD``),
    so callers must also confirm no ``.git`` exists before trusting it as a plain
    directory (see :func:`_has_dotgit_ancestor`).
    """
    return "not a git repository" in stderr.lower()


def _has_dotgit_ancestor(start: Path) -> bool:
    """True if a ``.git`` entry exists at ``start`` or any ancestor directory.

    Used only to tell a genuinely repository-free directory (safe to treat as
    untracked) apart from a present-but-corrupt ``.git`` that makes git report
    "not a git repository" while a tracked, repository-supplied session can still
    be on disk (provenance unknowable). A conservative lexical walk: it does not
    replicate git's full discovery rules, and any uncertainty fails closed by
    reporting that a ``.git`` may be present.
    """
    current = Path(os.path.abspath(start))
    while True:
        try:
            if os.path.lexists(current / ".git"):
                return True
        except OSError:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _ancestors_within(cwd: Path, path: Path) -> List[Path]:
    """Directory ancestors of ``path`` strictly between it and ``cwd``.

    Nearest first, excluding ``cwd`` and the leaf itself, and empty when ``path``
    is not under ``cwd``. Used to detect a repository-tracked ancestor (for
    example a submodule gitlink at ``.debugbrief``, or a committed parent
    directory) when the leaf session file itself is not directly in the index.
    """
    cwd_abs = Path(os.path.abspath(cwd))
    parent = Path(os.path.abspath(path)).parent
    ancestors: List[Path] = []
    while parent != cwd_abs and cwd_abs in parent.parents:
        ancestors.append(parent)
        parent = parent.parent
    return ancestors


def _ancestor_gitlink_state(cwd: Path, ancestor: Path, *, sanitized: bool) -> TrackedState:
    """Whether ``ancestor`` is itself a submodule gitlink index entry.

    A directory is never an index entry except as a submodule gitlink (mode
    ``160000``), so an exact gitlink at ``ancestor`` means the repository owns the
    session file beneath it. Tracked *descendant* files under a normal directory
    (for example a committed ``.debugbrief/README.md``) do not count: they do not
    make a separate, locally created session file repository-supplied.

    Returns ``TRACKED`` for an exact gitlink, ``UNTRACKED`` when ``ancestor`` is
    not an index entry, and ``UNKNOWN`` when the check could not run.
    """
    rel = os.path.relpath(os.path.abspath(ancestor), os.path.abspath(cwd))
    rc, out, _ = _run_git_rc(
        ["ls-files", "-sz", "--", rel],
        cwd,
        stable_locale=True,
        discover_from_cwd=sanitized,
    )
    if rc != 0:
        return TrackedState.UNKNOWN
    # ls-files -sz entries: "<mode> <sha> <stage>\t<path>" terminated by NUL.
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, tab, epath = entry.partition("\t")
        if tab and meta.split(" ", 1)[0] == "160000" and epath == rel:
            return TrackedState.TRACKED
    return TrackedState.UNTRACKED


def _classify_tracked(cwd: Path, path: Path, *, sanitized: bool) -> TrackedState:
    """Classify ``path`` under one git environment (see :func:`tracked_state`)."""
    # First decide whether we are inside a working tree, which also tells us
    # whether git works at all here.
    rc, out, err = _run_git_rc(
        ["rev-parse", "--is-inside-work-tree"],
        cwd,
        stable_locale=True,
        discover_from_cwd=sanitized,
    )
    if rc == 0:
        if out.strip() != "true":
            # Inside a bare repo or a .git directory: not a working tree, so there
            # is no checked-out, committable state path to worry about.
            return TrackedState.UNTRACKED
        rc, _, err = _run_git_rc(
            ["ls-files", "--error-unmatch", "--", str(path)],
            cwd,
            stable_locale=True,
            discover_from_cwd=sanitized,
        )
        if rc == 0:
            return TrackedState.TRACKED
        if rc == 1 and "did not match" in err.lower():
            # The leaf file is not directly in the index, but the repository can
            # still own it through a submodule gitlink ancestor: .debugbrief
            # shipped as a submodule means the parent index holds only the gitlink,
            # while the session lives in the submodule's checkout. Only an exact
            # gitlink ancestor counts; tracked sibling files under a normal
            # .debugbrief directory do not make this local session repo-supplied.
            for ancestor in _ancestors_within(cwd, path):
                state = _ancestor_gitlink_state(cwd, ancestor, sanitized=sanitized)
                if state is TrackedState.TRACKED:
                    return TrackedState.TRACKED
                if state is TrackedState.UNKNOWN:
                    return TrackedState.UNKNOWN
                # Not a gitlink: keep checking higher ancestors.
            return TrackedState.UNTRACKED
        # rc is None (git vanished mid-check), exit 128, or unexpected output.
        return TrackedState.UNKNOWN

    # Git could not establish a repository here: it could not run at all (rc is
    # None: not installed, timed out) or it failed (rc != 0). A genuinely
    # repository-free directory is safe regardless of why, because nothing could
    # have supplied committed state. But if a repository is plausibly present and
    # we simply could not read it (a present-but-broken .git, dubious ownership,
    # or an env-designated work tree), a tracked session may exist, so provenance
    # is unknowable and redo must fail closed. The sanitized pass ignores the
    # caller's env, so for it only a .git on disk counts.
    if _repository_plausibly_present(cwd, use_env=not sanitized):
        return TrackedState.UNKNOWN
    if rc is None or _is_not_a_repository(err):
        return TrackedState.UNTRACKED
    # git ran and failed for another reason without a discoverable repository.
    return TrackedState.UNKNOWN


def tracked_state(cwd: Path, path: Path) -> TrackedState:
    """Classify whether ``path`` is tracked in Git, failing closed when unsure.

    Returns:
    - ``UNTRACKED`` when ``cwd`` is not a Git repository at all, or it is and the
      path is confirmed absent from the index (``git ls-files`` exits 1 cleanly);
    - ``TRACKED`` when the path is in the index (``git ls-files`` exits 0);
    - ``UNKNOWN`` whenever provenance cannot be established: git is missing, times
      out, or fails for any reason other than a clean "not a repository" or
      "no match" (a fatal exit such as ``safe.directory`` ownership, a corrupt
      repository, a permission error, or unexpected output).

    The sanitized view ignores the caller's git env and discovers the repository
    from ``cwd``, so a stale or inherited ``GIT_DIR`` pointing elsewhere cannot
    hide a tracked file in the real repository. It is authoritative unless the
    environment could make git see different tracking (``_GIT_TRACKING_ENV``: a
    different repository, work tree, or index). When one of those is set, an
    inherited view that honors the caller's env is also consulted and the most
    restrictive result wins: ``TRACKED`` if either view sees the path tracked,
    otherwise ``UNKNOWN`` if either is unsure. This catches both a legitimately
    env-selected work tree (``GIT_DIR`` with ``GIT_WORK_TREE`` and no ``.git``
    marker) and a session tracked only in a caller-selected ``GIT_INDEX_FILE``.

    When git cannot establish a repository, whether to fail closed is keyed on
    whether a repository is actually present (see
    :func:`_repository_plausibly_present`), not merely on which variables are set,
    so a stale ``GIT_WORK_TREE`` in a plain directory does not refuse safe local
    state, while one pointing at a real checkout does. A discovery-only limiter
    such as ``GIT_CEILING_DIRECTORIES`` only restricts the upward search and is
    stripped for the sanitized view.
    """
    sanitized = _classify_tracked(cwd, path, sanitized=True)
    if not _git_tracking_env_present():
        return sanitized
    inherited = _classify_tracked(cwd, path, sanitized=False)
    if TrackedState.TRACKED in (sanitized, inherited):
        return TrackedState.TRACKED
    if TrackedState.UNKNOWN in (sanitized, inherited):
        return TrackedState.UNKNOWN
    return TrackedState.UNTRACKED


def is_tracked(cwd: Path, path: Path) -> bool:
    """True only if ``path`` is confirmed tracked in Git (see :func:`tracked_state`).

    Thin boolean view for callers that only care about the tracked case. Security
    decisions that must fail closed should use :func:`tracked_state` directly so
    an ``UNKNOWN`` result is not silently treated as untracked.
    """
    return tracked_state(cwd, path) is TrackedState.TRACKED


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
    ok, out, _ = _run_git(
        ["status", "--porcelain", "-z", "--untracked-files=all"], cwd
    )
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

    The regular-file branch opens with ``O_NOFOLLOW``/``O_NONBLOCK`` and verifies
    the descriptor with ``fstat``, so a file swapped to a symlink or FIFO between
    the ``lstat`` and the open is not followed and cannot block.
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
        with open_regular_binary(full) as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        # Unreadable, or swapped to a non-regular file after the lstat above.
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


def _parse_rename_map(out: str) -> Dict[str, str]:
    renames: Dict[str, str] = {}
    tokens = out.split("\x00")
    index = 0
    while index < len(tokens):
        code = tokens[index]
        if code[:1] in {"R", "C"} and index + 2 < len(tokens):
            source, destination = tokens[index + 1], tokens[index + 2]
            if source and destination:
                renames[source] = destination
            index += 3
            continue
        index += 1
    return renames


def _parse_status_rename_map(out: str) -> Dict[str, str]:
    renames: Dict[str, str] = {}
    tokens = out.split("\x00")
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        if entry[:1] in {"R", "C"} and index + 1 < len(tokens):
            source = tokens[index + 1]
            destination = entry[3:]
            if source and destination:
                renames[source] = destination
            index += 2
        else:
            index += 1
    return renames


def _status_rename_map(cwd: Path) -> Dict[str, str]:
    ok, out, _ = _run_git(
        ["status", "--porcelain", "-z", "--untracked-files=all"], cwd
    )
    return _parse_status_rename_map(out) if ok else {}


def _infer_baseline_renames(
    cwd: Path, baseline: Dict[str, str], candidates: Dict[str, str]
) -> Dict[str, str]:
    missing = [
        path
        for path in baseline
        if (
            path not in candidates or candidates.get(path) == "D"
        )
        and not os.path.lexists(cwd / path)
        and not _is_generated_artifact(path)
    ]
    additions = [
        path
        for path, label in candidates.items()
        if label == "A" and not _is_generated_artifact(path)
    ]
    sources_by_fingerprint: Dict[str, List[str]] = {}
    for path in missing:
        sources_by_fingerprint.setdefault(baseline[path], []).append(path)
    destinations_by_fingerprint: Dict[str, List[str]] = {}
    for path in additions:
        fingerprint = _file_fingerprint(cwd, path, deleted=False)
        destinations_by_fingerprint.setdefault(fingerprint, []).append(path)
    renames: Dict[str, str] = {}
    for fingerprint, destinations in destinations_by_fingerprint.items():
        sources = sources_by_fingerprint.get(fingerprint, [])
        if len(sources) == 1 and len(destinations) == 1:
            source, destination = sources[0], destinations[0]
            renames[source] = destination
            if candidates.get(source) == "D":
                candidates.pop(source, None)
    return renames


def _untracked_files(cwd: Path) -> List[str]:
    ok, out, _ = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd)
    if not ok:
        return []
    return [p for p in out.split("\x00") if p and not _is_generated_artifact(p)]


def _line_count(cwd: Path, path: str) -> int:
    try:
        with open_regular_binary(Path(cwd) / path) as handle:
            count = 0
            last = b""
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                count += chunk.count(b"\n")
                last = chunk[-1:]
            return count + (1 if last and last != b"\n" else 0)
    except (OSError, ValueError):
        return 0


def _parse_numstat_z(output: str) -> Dict[str, Tuple[int, int]]:
    result: Dict[str, Tuple[int, int]] = {}
    tokens = output.split("\x00")
    index = 0
    while index < len(tokens):
        header = tokens[index]
        fields = header.split("\t", 2)
        if len(fields) < 3:
            index += 1
            continue
        added = int(fields[0]) if fields[0].isdigit() else 0
        deleted = int(fields[1]) if fields[1].isdigit() else 0
        path = fields[2]
        index += 1
        if not path and index + 1 < len(tokens):
            source = tokens[index]
            destination = tokens[index + 1]
            path = destination or source
            index += 2
        if path and not _is_generated_artifact(path):
            result[path] = (added, deleted)
    return result


def _head_tree_paths(cwd: Path) -> List[str]:
    sha = current_sha(cwd)
    if not sha:
        return []
    ok, out, _ = _run_git(["ls-tree", "-r", "--name-only", "-z", sha], cwd)
    if not ok:
        return []
    return [path for path in out.split("\x00") if path and not _is_generated_artifact(path)]


def _root_commit_name_status(cwd: Path) -> List[Tuple[str, str]]:
    sha = current_sha(cwd)
    if not sha:
        return []
    ok, out, _ = _run_git(
        ["diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "-z", sha],
        cwd,
    )
    return _parse_diff_name_status(out) if ok else []


def _root_commit_numstat(cwd: Path) -> Dict[str, Tuple[int, int]]:
    sha = current_sha(cwd)
    if not sha:
        return {}
    ok, out, _ = _run_git(
        [
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--numstat",
            "-z",
            "--find-renames=20%",
            sha,
        ],
        cwd,
    )
    return _parse_numstat_z(out) if ok else {}


def _diff_numstat_by_paths(
    cwd: Path, initial_sha: Optional[str], paths: Optional[List[str]]
) -> Dict[str, Tuple[int, int]]:
    if paths is not None and not paths:
        return {}
    result: Dict[str, Tuple[int, int]] = {}
    chunks: List[Optional[List[str]]] = [paths] if paths is not None else [None]
    for chunk in chunks:
        if chunk is None:
            args = ["diff", "--numstat", "-z", "--find-renames=20%"]
            if initial_sha is not None:
                args.append(initial_sha)
        else:
            args = ["diff", "--numstat", "-z", "--find-renames=20%"]
            if initial_sha is not None:
                args.append(initial_sha)
            args.append("--")
            args.extend(chunk)
        ok, output, _ = _run_git(args, cwd)
        if ok:
            result.update(_parse_numstat_z(output))
    return result


def _session_line_delta(
    label: str,
    current: Tuple[int, int],
    baseline: Tuple[int, int],
    changed: bool = False,
) -> Tuple[int, int]:
    current_added, current_deleted = current
    baseline_added, baseline_deleted = baseline
    if label == "A":
        if current_added >= baseline_added:
            delta = (current_added - baseline_added, 0)
        else:
            delta = (0, baseline_added - current_added)
    elif label == "D":
        delta = (0, max(0, current_deleted - baseline_deleted) + baseline_added)
    else:
        delta = (
            max(0, current_added - baseline_added)
            + max(0, baseline_deleted - current_deleted),
            max(0, current_deleted - baseline_deleted)
            + max(0, baseline_added - current_added),
        )
    if changed and delta == (0, 0):
        return 1, 1
    return delta


def _session_file_changed(
    cwd: Path,
    path: str,
    label: str,
    baseline: Dict[str, str],
    rename_map: Dict[str, str],
    baseline_stats: Dict[str, Tuple[int, int]],
) -> bool:
    source = next((old for old, new in rename_map.items() if new == path), path)
    if source not in baseline and label == "R":
        return False
    if baseline.get(source) == _file_fingerprint(cwd, path, deleted=label == "D"):
        return False
    line_count = _line_count(cwd, path)
    baseline_added, baseline_deleted = baseline_stats.get(path, (0, 0))
    return line_count > 0 or baseline_added > 0 or baseline_deleted > 0


def _remap_baseline_stats(
    baseline_stats: Dict[str, Tuple[int, int]], rename_map: Dict[str, str]
) -> Dict[str, Tuple[int, int]]:
    remapped = dict(baseline_stats)
    for source, destination in rename_map.items():
        if source in baseline_stats and destination not in remapped:
            remapped[destination] = baseline_stats[source]
    return remapped


def _session_numstat(
    cwd: Path, initial_sha: Optional[str], paths: List[str]
) -> Dict[str, Tuple[int, int]]:
    result = _diff_numstat_by_paths(cwd, initial_sha, None)
    result = {path: result[path] for path in paths if path in result}
    untracked = set(_untracked_files(cwd))
    for path in paths:
        if path in untracked:
            result[path] = (_line_count(cwd, path), 0)
    return result


def working_tree_numstat(
    cwd: Path, initial_sha: Optional[str] = None
) -> Dict[str, Tuple[int, int]]:
    if initial_sha is not None:
        result = _diff_numstat_by_paths(cwd, initial_sha, None)
    else:
        result = _diff_numstat_by_paths(cwd, None, None)
        ok, staged_output, _ = _run_git(
            ["diff", "--cached", "--numstat", "-z"], cwd
        )
        staged = _parse_numstat_z(staged_output) if ok else {}
        for path, (added, deleted) in staged.items():
            prior_added, prior_deleted = result.get(path, (0, 0))
            result[path] = (prior_added + added, prior_deleted + deleted)
    if initial_sha is None:
        for _label, path in name_status(cwd):
            if os.path.lexists(cwd / path):
                result[path] = (_line_count(cwd, path), 0)
    for path in _untracked_files(cwd):
        result[path] = (_line_count(cwd, path), 0)
    return result


def _add_missing_baseline_paths(
    cwd: Path,
    baseline: Dict[str, str],
    candidates: Dict[str, str],
    skip_paths: Optional[Set[str]] = None,
) -> None:
    skipped = skip_paths or set()
    for path, baseline_fingerprint in baseline.items():
        if path in candidates or path in skipped or _is_generated_artifact(path):
            continue
        full_path = cwd / path
        if not os.path.lexists(full_path):
            current_fingerprint = "<deleted>"
            label = "D"
        else:
            current_fingerprint = _file_fingerprint(cwd, path, deleted=False)
            label = "M"
        if current_fingerprint != baseline_fingerprint:
            candidates[path] = label


def session_changes(
    cwd: Path,
    initial_sha: Optional[str],
    baseline: Dict[str, str],
    baseline_stats: Optional[Dict[str, Tuple[int, int]]] = None,
) -> Tuple[List[Tuple[str, str]], int, int]:
    """Files changed during the session, as ``(pairs, added, deleted)``.

    A file counts when it differs from the starting commit (committed or
    uncommitted) or is newly untracked, and its current content differs from its
    state at session start, so a file left dirty from before the session and
    untouched since is excluded. Lines are measured against the starting commit.
    Falls back to baseline fingerprints when there is no starting commit.
    """
    baseline_stats = baseline_stats or {}
    if not initial_sha:
        candidates: Dict[str, str] = {
            path: label for label, path in _root_commit_name_status(cwd)
        }
        for path in _head_tree_paths(cwd):
            candidates.setdefault(path, "A")
        for label, path in name_status(cwd):
            candidates[path] = label
        status_rename_map = _status_rename_map(cwd)
        status_rename_map.update(_infer_baseline_renames(cwd, baseline, candidates))
        _add_missing_baseline_paths(cwd, baseline, candidates, set(status_rename_map))
        pairs: List[Tuple[str, str]] = []
        for path, label in candidates.items():
            current = _file_fingerprint(cwd, path, deleted=(label == "D"))
            if baseline.get(path) != current:
                pairs.append((label, path))
        pairs.sort(key=lambda item: item[1])
        current_stats = working_tree_numstat(cwd)
        root_stats = _root_commit_numstat(cwd)
        for path, (root_added, _root_deleted) in root_stats.items():
            if path not in candidates:
                continue
            has_baseline = path in baseline or path in status_rename_map.values()
            if os.path.lexists(cwd / path):
                current_stats[path] = (_line_count(cwd, path), 0)
            elif has_baseline:
                current_stats[path] = (0, 0)
            else:
                current_stats[path] = (0, root_added)
        for path in candidates:
            if path not in current_stats and os.path.lexists(cwd / path):
                current_stats[path] = (_line_count(cwd, path), 0)
        effective_baseline_stats = _remap_baseline_stats(
            baseline_stats, status_rename_map
        )
        added = deleted = 0
        for label, path in pairs:
            added_delta, deleted_delta = _session_line_delta(
                label,
                current_stats.get(path, (0, 0)),
                effective_baseline_stats.get(path, (0, 0)),
                _session_file_changed(
                    cwd,
                    path,
                    label,
                    baseline,
                    status_rename_map,
                    effective_baseline_stats,
                ),
            )
            added += added_delta
            deleted += deleted_delta
        return pairs, added, deleted

    session_candidates: Dict[str, str] = {}
    rename_map: Dict[str, str] = {}
    ok, out, _ = _run_git(
        ["diff", "--name-status", "-z", "--find-renames=20%", initial_sha], cwd
    )
    if ok:
        rename_map = _parse_rename_map(out)
        for label, path in _parse_diff_name_status(out):
            session_candidates.setdefault(path, label)
    for path in _untracked_files(cwd):
        session_candidates.setdefault(path, "A")
    rename_map.update(_infer_baseline_renames(cwd, baseline, session_candidates))
    _add_missing_baseline_paths(cwd, baseline, session_candidates, set(rename_map))

    session_pairs: List[Tuple[str, str]] = []
    for path, label in session_candidates.items():
        current = _file_fingerprint(cwd, path, deleted=(label == "D"))
        if baseline.get(path) == current:
            continue  # already in this exact state before the session started
        session_pairs.append((label, path))
    session_pairs.sort(key=lambda item: item[1])

    tracked_paths = [path for _label, path in session_pairs]
    final_stats = _session_numstat(cwd, initial_sha, tracked_paths)
    effective_baseline_stats = _remap_baseline_stats(baseline_stats, rename_map)
    added = deleted = 0
    for label, path in session_pairs:
        added_delta, deleted_delta = _session_line_delta(
            label,
            final_stats.get(path, (0, 0)),
            effective_baseline_stats.get(path, (0, 0)),
            _session_file_changed(
                cwd, path, label, baseline, rename_map, effective_baseline_stats
            ),
        )
        added += added_delta
        deleted += deleted_delta
    return session_pairs, added, deleted


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
