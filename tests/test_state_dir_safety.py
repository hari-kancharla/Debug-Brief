"""Tests that DebugBrief refuses symlinked state directories and lock files.

A planted symlink at .debugbrief/ (or its sessions/reports subdirectories, or the
lock file) must never let DebugBrief read or write state outside the project.
"""

from __future__ import annotations

import os

import pytest

from debugbrief.paths import ProjectPaths, UnsafeStateDirectory
from debugbrief.session_manager import SessionManager

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="symlink semantics are POSIX-only"
)


def _paths(root):
    return ProjectPaths(project_root=root, is_git_repo=False, repo_root=None)


def test_symlinked_base_dir_is_rejected(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / ".debugbrief").symlink_to(tmp_path / "elsewhere")
    paths = _paths(tmp_path)
    with pytest.raises(UnsafeStateDirectory):
        paths.assert_state_dirs_safe()
    # And a real operation refuses too, rather than following the link.
    with pytest.raises(UnsafeStateDirectory):
        SessionManager(paths).start("t")


def test_symlinked_sessions_dir_is_rejected(tmp_path):
    base = tmp_path / ".debugbrief"
    base.mkdir()
    (tmp_path / "real_sessions").mkdir()
    (base / "sessions").symlink_to(tmp_path / "real_sessions")
    with pytest.raises(UnsafeStateDirectory):
        _paths(tmp_path).assert_state_dirs_safe()


def test_symlinked_reports_dir_is_rejected(tmp_path):
    base = tmp_path / ".debugbrief"
    base.mkdir()
    (tmp_path / "real_reports").mkdir()
    (base / "reports").symlink_to(tmp_path / "real_reports")
    with pytest.raises(UnsafeStateDirectory):
        _paths(tmp_path).assert_state_dirs_safe()


def test_non_directory_base_is_rejected(tmp_path):
    (tmp_path / ".debugbrief").write_text("not a dir", encoding="utf-8")
    with pytest.raises(UnsafeStateDirectory):
        _paths(tmp_path).assert_state_dirs_safe()


def test_symlinked_lock_file_is_rejected(tmp_path):
    paths = _paths(tmp_path)
    manager = SessionManager(paths)
    manager.start("t")  # creates the real state directories (start takes no lock)
    lock = paths.base_dir / ".lock"
    if lock.exists():
        lock.unlink()
    lock.symlink_to(tmp_path / "decoy")  # dangling symlink where the lock goes
    # add_note acquires the repo lock, which must refuse the symlinked lock file.
    with pytest.raises(UnsafeStateDirectory):
        manager.add_note("hello")
