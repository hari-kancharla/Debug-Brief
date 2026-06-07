"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from debugbrief.paths import ProjectPaths


def _git(args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def init_git_repo(path: Path) -> Path:
    """Initialize a git repo at ``path`` with one commit and return ``path``."""
    _git(["init", "-q"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test User"], path)
    _git(["checkout", "-q", "-b", "main"], path)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "seed.txt"], path)
    _git(["commit", "-q", "-m", "init"], path)
    return path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    return init_git_repo(tmp_path)


@pytest.fixture
def nogit_paths(tmp_path: Path) -> ProjectPaths:
    """A ProjectPaths rooted at a non-git temp dir."""
    return ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)


@pytest.fixture
def git_paths(git_repo: Path) -> ProjectPaths:
    return ProjectPaths(
        project_root=git_repo, is_git_repo=True, repo_root=git_repo
    )
