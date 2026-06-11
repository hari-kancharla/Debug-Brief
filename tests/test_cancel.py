"""Tests for `cancel`: discarding the active session without writing a report."""

from __future__ import annotations

import sys

import pytest

from debugbrief import cli
from debugbrief.models import SessionStatus
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


def _start_session(paths, title="cancel me"):
    manager = SessionManager(paths)
    session = manager.start(title)
    manager.add_note("an observation worth keeping")
    return session


def test_cancel_discards_active_session(paths, capsys):
    session = _start_session(paths)
    rc = cli.main(["cancel", "--yes"])
    assert rc == 0
    assert "Discarded session 'cancel me'" in capsys.readouterr().err

    # The pointer is gone, the session file survives with status ABANDONED.
    assert not paths.active_session_file.exists()
    stored = SessionManager(paths).load_session_file(session.session_id)
    assert stored.status == SessionStatus.ABANDONED.value
    assert stored.timestamps.end is not None
    # No report of any mode was written.
    assert not list(paths.reports_dir.glob("*")) or not any(
        p.stem.startswith(session.session_id) for p in paths.reports_dir.glob("*")
    )


def test_cancel_without_session_errors(paths, capsys):
    rc = cli.main(["cancel", "--yes"])
    assert rc == 1
    assert "No active DebugBrief session to cancel" in capsys.readouterr().err


def test_cancel_prompt_accepted(paths, monkeypatch):
    _start_session(paths)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert cli.main(["cancel"]) == 0
    assert not paths.active_session_file.exists()


def test_cancel_prompt_declined_leaves_everything_untouched(paths, monkeypatch, capsys):
    session = _start_session(paths)
    before = paths.session_file(session.session_id).read_text(encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    rc = cli.main(["cancel"])
    assert rc == 1
    assert "still active" in capsys.readouterr().err
    # Pointer still present, session file byte-identical, status still ACTIVE.
    assert paths.active_session_file.exists()
    assert paths.session_file(session.session_id).read_text(encoding="utf-8") == before
    assert SessionManager(paths).load_active().status == SessionStatus.ACTIVE.value


def test_cancel_prompt_eof_is_a_decline(paths, monkeypatch):
    _start_session(paths)

    def _raise_eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    assert cli.main(["cancel"]) == 1
    assert paths.active_session_file.exists()


def test_abandoned_session_appears_in_list_and_show(paths, capsys):
    session = _start_session(paths, title="abandoned but listed")
    cli.main(["cancel", "--yes"])
    capsys.readouterr()

    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ABANDONED]" in out
    assert "abandoned but listed" in out

    rc = cli.main(["show", session.session_id[:8]])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ABANDONED" in out
    assert "an observation worth keeping" in out
