"""Tests for the ``run -- <command>`` passthrough form and command reconstruction."""

from __future__ import annotations

import shlex
import sys

import pytest

from debugbrief import cli
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


def _stored_command(paths) -> str:
    session = SessionManager(paths).load_active()
    assert session is not None
    events = session.command_events()
    assert events, "no command event was recorded"
    return events[-1].data["command"]


def test_passthrough_runs_unquoted_command(paths, capsys):
    rc = cli.main(["run", "--", PY, "-c", "print('passthrough ok')"])
    assert rc == 0
    assert "passthrough ok" in capsys.readouterr().out
    tokens = shlex.split(_stored_command(paths))
    assert tokens[0] == PY
    assert tokens[-1] == "print('passthrough ok')"


def test_passthrough_flags_before_separator(paths):
    # -q after -- must reach the command, not be parsed as a DebugBrief flag.
    rc = cli.main(["run", "--timeout", "30", "--", PY, "-q", "-c", "pass"])
    assert rc == 0
    assert "-q" in shlex.split(_stored_command(paths))


def test_passthrough_preserves_args_with_spaces(paths):
    rc = cli.main(["run", "--", PY, "-c", "print('a b c')"])
    assert rc == 0
    # The argument containing spaces survives storage as a single token.
    assert shlex.split(_stored_command(paths)) == [PY, "-c", "print('a b c')"]


def test_quoted_form_still_works(paths):
    cmd = f"{PY} -c \"print('quoted form')\""
    rc = cli.main(["run", cmd])
    assert rc == 0
    # The single quoted argument is stored verbatim, exactly as before.
    assert _stored_command(paths) == cmd


def test_run_with_no_command_errors(paths, capsys):
    rc = cli.main(["run", "--"])
    assert rc == 2
    assert "No command given" in capsys.readouterr().err


def test_reconstruct_round_trips_through_shlex():
    parts = ["--", "python", "-c", "print('a b')"]
    rebuilt = cli._reconstruct_command(parts)
    assert shlex.split(rebuilt) == ["python", "-c", "print('a b')"]
    # Single token (quoted form) is preserved verbatim.
    assert cli._reconstruct_command(["pytest -q"]) == "pytest -q"
    assert cli._reconstruct_command([]) == ""
    assert cli._reconstruct_command(["--"]) == ""
