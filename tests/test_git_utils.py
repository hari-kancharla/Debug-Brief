"""Tests for the native-git wrappers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from debugbrief import git_utils


def _git(args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_outside_repo_is_safe(tmp_path):
    assert git_utils.find_repo_root(tmp_path) is None
    assert git_utils.is_inside_repo(tmp_path) is False
    state = git_utils.capture_state(tmp_path, initial=True)
    assert state.is_repo is False
    assert state.repo_root is None
    assert git_utils.changed_files(tmp_path) == []
    assert git_utils.shortstat(tmp_path) == (0, 0)


def test_inside_repo_basic(git_repo):
    assert git_utils.is_inside_repo(git_repo) is True
    assert git_utils.find_repo_root(git_repo) is not None
    assert git_utils.current_branch(git_repo) == "main"
    assert git_utils.is_detached_head(git_repo) is False
    sha = git_utils.current_sha(git_repo)
    assert sha and len(sha) >= 7


def test_capture_initial_and_final(git_repo):
    initial = git_utils.capture_state(git_repo, initial=True)
    assert initial.is_repo is True
    assert initial.initial_sha is not None
    assert initial.final_sha is None
    assert initial.branch == "main"

    final = git_utils.capture_state(git_repo, initial=False)
    assert final.final_sha is not None
    assert final.initial_sha is None


def test_changed_files_and_shortstat(git_repo):
    target = git_repo / "seed.txt"
    target.write_text("seed\nmore\n", encoding="utf-8")
    (git_repo / "new.txt").write_text("brand new\n", encoding="utf-8")

    files = git_utils.changed_files(git_repo)
    assert "seed.txt" in files
    assert "new.txt" in files

    added, deleted = git_utils.shortstat(git_repo)
    assert added >= 1
    assert deleted == 0


def test_detached_head(git_repo):
    sha = git_utils.current_sha(git_repo)
    _git(["checkout", "-q", sha], git_repo)
    assert git_utils.is_detached_head(git_repo) is True
    assert git_utils.current_branch(git_repo) is None
    state = git_utils.capture_state(git_repo, initial=True)
    assert state.detached_head is True
    assert state.branch is None


def test_name_status_labels(git_repo):
    # Modify a tracked file, add a new file, and delete a tracked file.
    (git_repo / "seed.txt").write_text("seed\nmodified\n", encoding="utf-8")
    (git_repo / "added.txt").write_text("brand new\n", encoding="utf-8")
    (git_repo / "to_delete.txt").write_text("temp\n", encoding="utf-8")
    _git(["add", "to_delete.txt"], git_repo)
    _git(["commit", "-q", "-m", "add file to delete"], git_repo)
    (git_repo / "to_delete.txt").unlink()

    pairs = dict((path, label) for label, path in git_utils.name_status(git_repo))
    assert pairs["seed.txt"] == "M"
    assert pairs["added.txt"] == "A"
    assert pairs["to_delete.txt"] == "D"

    # changed_files is derived from name_status and lists the same paths.
    files = git_utils.changed_files(git_repo)
    assert set(files) == set(pairs.keys())


def test_name_status_outside_repo_is_empty(tmp_path):
    assert git_utils.name_status(tmp_path) == []


def test_porcelain_label_mapping():
    assert git_utils._porcelain_label("??") == "A"
    assert git_utils._porcelain_label("M ") == "M"
    assert git_utils._porcelain_label(" M") == "M"
    assert git_utils._porcelain_label("A ") == "A"
    assert git_utils._porcelain_label("D ") == "D"
    assert git_utils._porcelain_label("R ") == "R"
    assert git_utils._porcelain_label("C ") == "A"


def test_parse_shortstat_helper():
    text = " 3 files changed, 12 insertions(+), 4 deletions(-)"
    assert git_utils._parse_shortstat(text) == (12, 4)
    assert git_utils._parse_shortstat(" 1 file changed, 2 insertions(+)") == (2, 0)
    assert git_utils._parse_shortstat(" 1 file changed, 5 deletions(-)") == (0, 5)
