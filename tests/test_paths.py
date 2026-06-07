"""Tests for project-root detection, storage layout, and local ignore handling."""

from __future__ import annotations

from pathlib import Path

from debugbrief import paths as paths_mod
from debugbrief.paths import (
    ProjectPaths,
    ensure_local_ignore,
    resolve_project_paths,
)


def test_resolve_outside_git_uses_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolved = resolve_project_paths()
    assert resolved.is_git_repo is False
    assert resolved.repo_root is None
    assert resolved.project_root == tmp_path.resolve()


def test_resolve_inside_git_uses_repo_root(git_repo, monkeypatch):
    subdir = git_repo / "nested" / "deeper"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    resolved = resolve_project_paths()
    assert resolved.is_git_repo is True
    assert resolved.project_root.resolve() == git_repo.resolve()


def test_storage_layout_paths(tmp_path):
    pp = ProjectPaths(project_root=tmp_path, is_git_repo=False)
    assert pp.base_dir == tmp_path / ".debugbrief"
    assert pp.active_session_file.name == "active_session.json"
    assert pp.session_file("abc").parts[-2:] == ("sessions", "abc.json")
    assert pp.report_file("abc", "pr").parts[-2:] == ("reports", "abc-pr.md")


def test_ensure_directories_creates_tree(tmp_path):
    pp = ProjectPaths(project_root=tmp_path, is_git_repo=False)
    pp.ensure_directories()
    assert pp.base_dir.is_dir()
    assert pp.sessions_dir.is_dir()
    assert pp.reports_dir.is_dir()


def test_ensure_local_ignore_in_git_repo(git_paths):
    changed, warnings = ensure_local_ignore(git_paths)
    assert changed is True
    assert warnings == []
    exclude = git_paths.repo_root / ".git" / "info" / "exclude"
    contents = exclude.read_text(encoding="utf-8")
    assert ".debugbrief/" in contents

    # Idempotent: a second call should not add a duplicate entry.
    changed_again, _ = ensure_local_ignore(git_paths)
    assert changed_again is False
    assert contents.count(".debugbrief/") == 1


def test_ensure_local_ignore_outside_git_is_noop(nogit_paths):
    changed, warnings = ensure_local_ignore(nogit_paths)
    assert changed is False
    assert warnings == []


def test_ensure_local_ignore_warns_when_git_is_file(tmp_path):
    # Simulate a worktree/submodule where .git is a file, not a directory.
    (tmp_path / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    pp = ProjectPaths(project_root=tmp_path, is_git_repo=True, repo_root=tmp_path)
    changed, warnings = ensure_local_ignore(pp)
    assert changed is False
    assert warnings and "worktree" in warnings[0]
