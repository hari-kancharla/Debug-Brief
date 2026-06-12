"""Tests for the expanded runner table, --verify, and preview."""

from __future__ import annotations

import sys

import pytest

from debugbrief import cli, filters
from debugbrief.paths import ProjectPaths
from debugbrief.session_manager import SessionManager

PY = sys.executable


@pytest.fixture
def paths(tmp_path):
    return ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)


@pytest.fixture(autouse=True)
def _patch_resolve(monkeypatch, paths):
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    return paths


# Expanded runner table -------------------------------------------------------
@pytest.mark.parametrize(
    ("command", "tool"),
    [
        ("vitest run", "vitest"),
        ("npx vitest", "vitest"),
        ("bun test", "bun"),
        ("deno test --allow-read", "deno"),
        ("node --test tests/", "node"),
        ("make test", "make"),
        ("make check", "make"),
        ("tox -e py311", "tox"),
        ("python -m unittest discover", "unittest"),
        ("dotnet test", "dotnet"),
        ("ctest --output-on-failure", "ctest"),
        ("phpunit tests/", "phpunit"),
        ("mix test", "mix"),
        ("swift test", "swift"),
    ],
)
def test_new_runners_classified(command, tool):
    passing = filters.classify_command(command, exit_code=0)
    assert passing.is_test is True, command
    assert passing.tool == tool, command
    assert passing.is_verification is True, command

    failing = filters.classify_command(command, exit_code=1)
    assert failing.is_test is True, command
    assert failing.tool == tool, command
    assert failing.is_verification is False, command


@pytest.mark.parametrize("command", ["make build", "bun install", "deno run app.ts"])
def test_non_test_invocations_not_classified(command):
    cls = filters.classify_command(command, exit_code=0)
    assert cls.is_test is False, command
    assert cls.tool is None or cls.tool not in ("make", "bun", "deno"), command


# --verify --------------------------------------------------------------------
def test_verify_marks_custom_command_as_check():
    cls = filters.classify_command(
        "./scripts/integration.sh", exit_code=0, force_verification=True
    )
    assert cls.tool == "custom"
    assert cls.is_test is False
    assert cls.is_verification is True


def test_verify_failure_stays_honest():
    cls = filters.classify_command(
        "./scripts/integration.sh", exit_code=1, force_verification=True
    )
    assert cls.tool == "custom"
    assert cls.is_verification is False


def test_verify_is_noop_on_recognized_runner():
    cls = filters.classify_command("pytest -q", exit_code=0, force_verification=True)
    assert cls.tool == "pytest"
    assert cls.is_test is True


def test_verify_enables_red_to_green_for_custom_check(paths):
    # A custom script fails, then passes: with --verify both runs are
    # verification candidates, so the report derives reproduce/verify lines.
    script = paths.project_root / "check.sh"
    script.write_text("#!/bin/sh\nexit $(cat flag)\n", encoding="utf-8")
    script.chmod(0o755)
    flag = paths.project_root / "flag"

    flag.write_text("1", encoding="utf-8")
    assert cli.main(["run", "--verify", "--", "./check.sh"]) == 1
    flag.write_text("0", encoding="utf-8")
    assert cli.main(["run", "--verify", "--", "./check.sh"]) == 0

    report = SessionManager(paths).preview("pr")
    assert "Reproduce (failed): `./check.sh`" in report
    assert "Verify (passed): `./check.sh`" in report


def test_redo_inherits_verify(paths):
    (paths.project_root / "ok.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (paths.project_root / "ok.sh").chmod(0o755)
    assert cli.main(["run", "--verify", "--", "./ok.sh"]) == 0
    assert cli.main(["redo"]) == 0

    events = SessionManager(paths).load_active().command_events()
    assert len(events) == 2
    assert events[1].data["classification"]["tool"] == "custom"
    assert events[1].data["classification"]["is_verification"] is True


# preview ---------------------------------------------------------------------
def test_preview_renders_without_mutating(paths, capsys):
    manager = SessionManager(paths)
    session = manager.start("preview me")
    manager.add_note("an observation")
    before = paths.session_file(session.session_id).read_bytes()
    capsys.readouterr()

    rc = cli.main(["preview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("# preview me")
    assert "Preview of an active session" in out
    assert "an observation" in out

    # Nothing changed on disk: session byte-identical, no reports written.
    assert paths.session_file(session.session_id).read_bytes() == before
    assert manager.load_active().status == "ACTIVE"
    assert not paths.reports_dir.exists() or not list(paths.reports_dir.glob("*"))


def test_preview_mid_session_with_commands(paths, capsys):
    cli.main(["run", "--", PY, "-c", "print(42)"])
    capsys.readouterr()
    rc = cli.main(["preview", "--mode", "incident"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Preview of an active session" in out
    assert "print(42)" in out


def test_preview_without_session_errors(paths, capsys):
    rc = cli.main(["preview"])
    assert rc == 1
    assert "No active DebugBrief session" in capsys.readouterr().err
