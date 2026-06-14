"""Tests for `redo`, the lazier `end` (default mode, --stdout), and unquoted notes."""

from __future__ import annotations

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


def _active_session(paths):
    session = SessionManager(paths).load_active()
    assert session is not None
    return session


# redo -----------------------------------------------------------------------
def test_redo_reruns_last_command(paths, capsys):
    rc = cli.main(["run", "--", PY, "-c", "print('again please')"])
    assert rc == 0
    capsys.readouterr()

    rc = cli.main(["redo"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "again please" in captured.out  # the rerun streamed its output
    assert "(redo)" in captured.err

    events = _active_session(paths).command_events()
    assert len(events) == 2
    assert events[0].data["command"] == events[1].data["command"]
    assert events[1].data["exit_code"] == 0


def test_redo_propagates_failure_exit_code(paths):
    cli.main(["run", "--", PY, "-c", "import sys; sys.exit(3)"])
    assert cli.main(["redo"]) == 3


def test_redo_without_session_errors(paths, capsys):
    rc = cli.main(["redo"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No active DebugBrief session" in err
    assert "debugbrief run" in err


def test_redo_without_commands_errors(paths, capsys):
    cli.main(["note", "only a note so far"])
    capsys.readouterr()
    rc = cli.main(["redo"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No commands have been captured" in err


def test_redo_refuses_redacted_command(paths, capsys):
    # The secret in the command text is masked on store, so the stored command
    # is a placeholder, not something that can honestly be re-run.
    rc = cli.main(["run", "--", PY, "-c", "print('sk-abcdefghijklmnop1234')"])
    assert rc == 0
    stored = _active_session(paths).command_events()[-1].data["command"]
    assert "[redacted]" in stored
    capsys.readouterr()

    rc = cli.main(["redo"])
    assert rc == 1
    assert "cannot be re-run" in capsys.readouterr().err
    # No new command event was recorded.
    assert len(_active_session(paths).command_events()) == 1


def test_redo_keeps_original_shell_mode(paths):
    rc = cli.main(["run", "--shell", "echo from-the-shell | tr a-z A-Z"])
    assert rc == 0
    assert cli.main(["redo"]) == 0
    events = _active_session(paths).command_events()
    assert len(events) == 2
    assert events[1].data["used_shell"] is True


def test_redo_runs_in_original_directory(paths, tmp_path, monkeypatch, capsys):
    # In a monorepo a command captured in one package must re-run there, even when
    # `redo` is invoked from a different directory.
    dir_a = tmp_path / "pkg_a"
    dir_b = tmp_path / "pkg_b"
    dir_a.mkdir()
    dir_b.mkdir()
    monkeypatch.chdir(dir_a)
    assert cli.main(["run", "--", PY, "-c", "import os; print(os.getcwd())"]) == 0
    run_cwd = _active_session(paths).command_events()[-1].data["invocation_cwd"]
    capsys.readouterr()

    monkeypatch.chdir(dir_b)
    assert cli.main(["redo"]) == 0
    out = capsys.readouterr().out
    assert run_cwd in out  # the rerun printed the ORIGINAL directory, not dir_b
    assert _active_session(paths).command_events()[-1].data["invocation_cwd"] == run_cwd


def test_redo_fails_when_original_directory_is_gone(paths, tmp_path, monkeypatch, capsys):
    import shutil

    work = tmp_path / "transient"
    work.mkdir()
    monkeypatch.chdir(work)
    assert cli.main(["run", "--", PY, "-c", "print('hi')"]) == 0
    capsys.readouterr()

    monkeypatch.chdir(tmp_path)
    shutil.rmtree(work)
    rc = cli.main(["redo"])
    assert rc == 1
    assert "no longer exists" in capsys.readouterr().err
    # The command was not re-run, so no new event was recorded.
    assert len(_active_session(paths).command_events()) == 1


def test_redo_warns_when_directory_not_recorded(paths, capsys):
    # Older session data may predate invocation_cwd; redo falls back to the current
    # directory and says so rather than failing or guessing silently.
    assert cli.main(["run", "--", PY, "-c", "print('hi')"]) == 0
    sm = SessionManager(paths)
    session = sm.load_active()
    session.events[-1].data["invocation_cwd"] = None
    sm.save_session(session)
    capsys.readouterr()

    rc = cli.main(["redo"])
    assert rc == 0
    assert "directory was not recorded" in capsys.readouterr().err


# end ------------------------------------------------------------------------
def test_end_defaults_to_pr_mode(paths, capsys):
    cli.main(["run", "--", PY, "-c", "print('x')"])
    session_id = _active_session(paths).session_id
    rc = cli.main(["end"])
    assert rc == 0
    assert paths.report_file(session_id, "pr").exists()
    assert "mode:      pr" in capsys.readouterr().out


def test_end_stdout_pipes_report_and_moves_info_to_stderr(paths, capsys):
    cli.main(["run", "--", PY, "-c", "print('x')"])
    session_id = _active_session(paths).session_id
    capsys.readouterr()

    rc = cli.main(["end", "--stdout"])
    assert rc == 0
    captured = capsys.readouterr()
    # stdout carries exactly the markdown report that was written to disk.
    report_text = paths.report_file(session_id, "pr").read_text(encoding="utf-8")
    assert captured.out == report_text
    # All informational lines went to stderr.
    assert "Session completed" in captured.err
    assert "Session completed" not in captured.out


# note -----------------------------------------------------------------------
def test_note_unquoted_tokens_join(paths):
    rc = cli.main(["note", "remember", "to", "check", "the", "lock", "ordering"])
    assert rc == 0
    notes = _active_session(paths).note_events()
    assert notes[-1].data["text"] == "remember to check the lock ordering"


def test_note_quoted_form_still_works(paths):
    rc = cli.main(["note", "a single quoted note"])
    assert rc == 0
    notes = _active_session(paths).note_events()
    assert notes[-1].data["text"] == "a single quoted note"
