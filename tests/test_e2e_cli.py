"""End-to-end test driving the real CLI entrypoint via subprocess.

Unlike the other tests, this exercises the installed/importable CLI as a black
box (``python -m debugbrief ...``) against a real temporary Git repository.
"""

from __future__ import annotations

import subprocess
import sys

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
    (tmp_path / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8"
    )
    _git(["add", "app.py", "test_smoke.py"], tmp_path)
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

    verify = _cli(["run", f"{sys.executable} -m pytest -q"], repo)
    assert verify.returncode == 0, verify.stderr

    end = _cli(["end", "--mode", "pr"], repo)
    assert end.returncode == 0, end.stderr

    # Resolve to avoid macOS /var vs /private/var symlink mismatches.
    reports_dir = repo.resolve() / ".debugbrief" / "reports"
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
    # Verification section (the real pytest command exited 0)
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


def test_subdirectory_replay_exports_the_directory_needed_to_reproduce(repo):
    import json

    package = repo / "package"
    package.mkdir()
    (package / "test_location.py").write_text(
        "from pathlib import Path\ndef test_location():\n    assert Path.cwd().name == 'package'\n"
    )
    result = _cli(["run", "--", sys.executable, "-m", "pytest", "-q", "test_location.py"], package)
    assert result.returncode == 0, result.stderr
    replay = _cli(["redo"], repo)
    assert replay.returncode == 0, replay.stderr
    assert _cli(["end", "--format", "all"], repo).returncode == 0
    payload = json.loads(next((repo / ".debugbrief/reports").glob("*-pr.json")).read_text())
    assert [c["working_directory"] for c in payload["commands"]] == ["package", "package"]
    assert payload["verify_working_directory"] == "package"
    assert payload["verification"][0]["working_directory"] == "package"
    assert payload["evidence"]["state"] == "verified"


def test_broken_pipe_exits_cleanly(tmp_path):
    # A consumer that closes our stdout early (debugbrief show ... | head -1)
    # must not produce a traceback; the CLI exits with 141 (128 + SIGPIPE).
    from debugbrief.paths import ProjectPaths
    from debugbrief.session_manager import SessionManager

    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    manager = SessionManager(paths)
    session = manager.start("big output session")
    # Enough note bytes that the JSON dump overflows the OS pipe buffer, so the
    # write actually fails once the read end is closed.
    for _ in range(5):
        manager.add_note("x" * 20000)

    with subprocess.Popen(
        [sys.executable, "-m", "debugbrief", "show", session.session_id, "--json"],
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        assert proc.stdout is not None and proc.stderr is not None
        proc.stdout.close()  # simulate `| head -1` exiting immediately
        stderr = proc.stderr.read()
        rc = proc.wait()

    assert "Traceback" not in stderr, stderr
    assert "BrokenPipeError" not in stderr, stderr
    assert rc == 141


def test_real_failed_then_fixed_check_exports_inspectable_history(repo):
    import json

    (repo / "test_smoke.py").write_text("def test_smoke():\n    assert False\n", encoding="utf-8")
    failure = _cli(["run", "--", sys.executable, "-m", "pytest", "-q"], repo)
    assert failure.returncode == 1
    (repo / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    passed = _cli(["redo"], repo)
    assert passed.returncode == 0
    exported = _cli(["end", "--format", "all"], repo)
    assert exported.returncode == 0, exported.stderr
    reports = repo / ".debugbrief/reports"
    payload = json.loads(next(reports.glob("*-pr.json")).read_text())
    assert payload["schema_version"] == 1
    assert [attempt["exit_code"] for attempt in payload["commands"]] == [1, 0]
    assert "AssertionError" in payload["commands"][0]["stdout_preview"]
    assert payload["commands"][1]["is_verification"] is True
    assert payload["evidence"]["state"] == "verified"
    html = next(reports.glob("*-pr.html")).read_text()
    assert "Command output" in html and "AssertionError" in html
    assert next(reports.glob("*-pr.md")).is_file()


def test_file_unchanged_between_checks_is_not_attributed_to_the_fix_window(repo):
    import json

    assert _cli(['start', 'File correlation check'], repo).returncode == 0
    (repo / 'app.py').write_text('x = 2\n', encoding='utf-8')
    (repo / 'test_smoke.py').write_text('def test_smoke():\n    assert False\n', encoding='utf-8')
    assert _cli(['run', '--', sys.executable, '-m', 'pytest', '-q'], repo).returncode == 1
    (repo / 'test_smoke.py').write_text('def test_smoke():\n    assert 1 + 1 == 2\n', encoding='utf-8')
    assert _cli(['redo'], repo).returncode == 0
    assert _cli(['end', '--format', 'json'], repo).returncode == 0
    report = next((repo / '.debugbrief/reports').glob('*.json'))
    payload = json.loads(report.read_text())
    assert payload['red_to_green']['changed_files'] == ['test_smoke.py']
    assert {item['path'] for item in payload['changed_files']} == {'app.py', 'test_smoke.py'}
