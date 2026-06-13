"""Tests for the native-git wrappers."""

from __future__ import annotations

import subprocess

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

    pairs = {path: label for label, path in git_utils.name_status(git_repo)}
    assert pairs["seed.txt"] == "M"
    assert pairs["added.txt"] == "A"
    assert pairs["to_delete.txt"] == "D"

    # changed_files is derived from name_status and lists the same paths.
    files = git_utils.changed_files(git_repo)
    assert set(files) == set(pairs.keys())


def test_name_status_outside_repo_is_empty(tmp_path):
    assert git_utils.name_status(tmp_path) == []


def test_name_status_handles_unicode_spaces_and_renames(git_repo):
    # Non-ASCII and spaced filenames must come through verbatim, not C-quoted
    # and octal-escaped (caf\303\251.txt), and a rename must report the new name.
    (git_repo / "café_漢字.txt").write_text("x\n", encoding="utf-8")
    (git_repo / "file with spaces.txt").write_text("y\n", encoding="utf-8")
    _git(["mv", "seed.txt", "renamed_café.txt"], git_repo)

    pairs = {path: label for label, path in git_utils.name_status(git_repo)}
    assert "café_漢字.txt" in pairs
    assert pairs["café_漢字.txt"] == "A"
    assert "file with spaces.txt" in pairs
    assert pairs["renamed_café.txt"] == "R"
    # No octal-escaped or quote-wrapped artifacts leaked in.
    assert all("\\3" not in path and '"' not in path for path in pairs)


def test_name_status_excludes_generated_artifacts(git_repo):
    # A real source change that must survive the filtering.
    (git_repo / "real.py").write_text("print('hi')\n", encoding="utf-8")

    # Generated/cache artifacts. Fully-untracked directories are reported by
    # porcelain as a single directory entry, while individual untracked files
    # (here the bare .pyo at the repo root) are reported by path.
    cache = git_repo / "__pycache__"
    cache.mkdir()
    (cache / "real.cpython-39.pyc").write_text("x", encoding="utf-8")
    (git_repo / "module.pyo").write_text("x", encoding="utf-8")

    egg = git_repo / "pkg.egg-info"
    egg.mkdir()
    (egg / "SOURCES.txt").write_text("real.py\n", encoding="utf-8")

    for cache_dir in (".pytest_cache", ".mypy_cache", ".ruff_cache"):
        d = git_repo / cache_dir
        d.mkdir()
        (d / "entry").write_text("x", encoding="utf-8")

    files = git_utils.changed_files(git_repo)
    assert "real.py" in files
    assert all(
        not git_utils._is_generated_artifact(path) for path in files
    ), files
    # None of the artifact paths should leak into the change summary.
    joined = "\n".join(files)
    for needle in ("__pycache__", ".pyo", ".egg-info", ".pytest_cache",
                   ".mypy_cache", ".ruff_cache"):
        assert needle not in joined


def test_is_generated_artifact_helper():
    assert git_utils._is_generated_artifact("pkg/__pycache__/mod.cpython-39.pyc")
    assert git_utils._is_generated_artifact("build/foo.pyo")
    assert git_utils._is_generated_artifact("src/thing.egg-info/PKG-INFO")
    assert git_utils._is_generated_artifact(".mypy_cache/3.9/foo.json")
    assert git_utils._is_generated_artifact(".pytest_cache/v/cache/lastfailed")
    assert git_utils._is_generated_artifact("frontend/node_modules/pkg/index.js")
    assert git_utils._is_generated_artifact("docs/.DS_Store")
    # Real source files must never be filtered, even when the name contains a
    # sensitive-looking token.
    assert not git_utils._is_generated_artifact("src/debugbrief/redaction.py")
    assert not git_utils._is_generated_artifact("src/app/keymap.py")
    assert not git_utils._is_generated_artifact("src/app/api_client.py")
    assert not git_utils._is_generated_artifact("README.md")
    # "egg-info" must be a full segment suffix, not an embedded substring.
    assert not git_utils._is_generated_artifact("my.egg-info.txt")


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
