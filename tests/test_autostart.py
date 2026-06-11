"""Tests for auto-starting a session on `run` / `note` with none active."""

from __future__ import annotations

import argparse
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


def test_note_autostarts_session(paths, capsys):
    assert not paths.active_session_file.exists()
    rc = cli.cmd_note(argparse.Namespace(text="first observation"))
    assert rc == 0
    # The notice is a status line, kept on stderr.
    err = capsys.readouterr().err
    assert "Auto-started" in err

    manager = SessionManager(paths)
    session = manager.load_active()
    assert session is not None
    assert len(session.note_events()) == 1
    assert session.note_events()[0].data["text"] == "first observation"


def test_run_autostarts_session(paths, capsys):
    assert not paths.active_session_file.exists()
    rc = cli.cmd_run(
        argparse.Namespace(
            command=[f"{PY} -c \"print('hi')\""],
            shell=False,
            timeout=30,
            no_redact=False,
        )
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "Auto-started" in err

    manager = SessionManager(paths)
    session = manager.load_active()
    assert session is not None
    assert len(session.command_events()) == 1


def test_existing_session_is_not_replaced(paths, capsys):
    manager = SessionManager(paths)
    started = manager.start("explicit title")
    capsys.readouterr()  # clear

    cli.cmd_note(argparse.Namespace(text="note into existing"))
    captured = capsys.readouterr()
    assert "Auto-started" not in captured.out + captured.err

    reloaded = manager.load_active()
    assert reloaded.session_id == started.session_id
    assert reloaded.title == "explicit title"
