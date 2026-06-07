"""End-to-end test driving the real CLI entrypoint via subprocess.

Unlike the other tests, this exercises the installed/importable CLI as a black
box (``python -m debugbrief ...``) against a real temporary Git repository.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def _git(args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _cli(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "debugbrief", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "e2e@example.com"], tmp_path)
    _git(["config", "user.name", "E2E"], tmp_path)
    _git(["checkout", "-q", "-b", "main"], tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "app.py"], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)
    return tmp_path


def test_full_cli_flow(repo):
    note_text = "Investigating the failing refresh path."

    start = _cli(["start", "E2E debug session"], repo)
    assert start.returncode == 0, start.stderr

    note = _cli(["note", note_text], repo)
    assert note.returncode == 0, note.stderr

    # Modify a tracked file so the report has a real changed-files section.
    (repo / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    passing = _cli(["run", f"{sys.executable} -c 'print(123)'"], repo)
    assert passing.returncode == 0, passing.stderr

    verify = _cli(["run", f"{sys.executable} -m pytest --version"], repo)
    assert verify.returncode == 0, verify.stderr

    end = _cli(["end", "--mode", "pr"], repo)
    assert end.returncode == 0, end.stderr

    # Resolve to avoid macOS /var vs /private/var symlink mismatches.
    reports_dir = (repo.resolve() / ".debugbrief" / "reports")
    reports = list(reports_dir.glob("*-pr.md"))
    assert len(reports) == 1
    report_path = reports[0]
    content = report_path.read_text(encoding="utf-8")

    # Title
    assert "# E2E debug session" in content
    # Note text
    assert note_text in content
    # Command
    assert "print(123)" in content
    # Exit code / pass status
    assert "passed" in content
    assert "exit 0" in content
    # Modified file
    assert "app.py" in content
    # Verification section (pytest --version is a passing verification command)
    assert "## Verification and tests" in content
    assert "[passed]" in content

    # last returns the latest report
    last = _cli(["last"], repo)
    assert last.returncode == 0, last.stderr
    assert report_path.name in last.stdout
    assert "pr" in last.stdout


def test_cli_help_and_version(repo):
    help_result = _cli(["--help"], repo)
    assert help_result.returncode == 0
    assert "debugbrief" in help_result.stdout
    for command in ["start", "note", "run", "end", "status", "list", "show"]:
        assert command in help_result.stdout

    version = _cli(["--version"], repo)
    assert version.returncode == 0
    assert version.stdout.strip()
