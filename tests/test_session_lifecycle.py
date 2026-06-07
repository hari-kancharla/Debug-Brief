"""Tests for the full session lifecycle and persistence."""

from __future__ import annotations

import sys

import pytest

from debugbrief.command_runner import run_command
from debugbrief.models import Session, SessionStatus
from debugbrief.session_manager import SessionError, SessionManager
from debugbrief.utils import read_json

PY = sys.executable


def _record_run(manager, command, **kwargs):
    result = run_command(command, cwd=manager.paths.project_root, **kwargs)
    manager.record_command(result)
    return result


def test_full_lifecycle(nogit_paths):
    manager = SessionManager(nogit_paths)

    session = manager.start("Fix the thing")
    assert session.status == SessionStatus.ACTIVE.value
    assert nogit_paths.active_session_file.exists()
    assert nogit_paths.session_file(session.session_id).exists()

    manager.add_note("First observation")
    _record_run(manager, f"{PY} -c \"print('hi')\"")

    completed = manager.end("pr")
    assert completed.status == SessionStatus.COMPLETED.value
    assert completed.timestamps.end is not None

    # Active pointer removed after a clean end.
    assert not nogit_paths.active_session_file.exists()
    # Report written.
    report_path = nogit_paths.report_file(completed.session_id, "pr")
    assert report_path.exists()
    assert "# Fix the thing" in report_path.read_text(encoding="utf-8")
    # Capture status is honest "full" for the explicit-run model.
    assert completed.summary.command_capture_status == "full"


def test_counts_update(nogit_paths):
    manager = SessionManager(nogit_paths)
    manager.start("counting")
    manager.add_note("a")
    manager.add_note("b")
    _record_run(manager, f"{PY} -c \"print(1)\"")
    _record_run(manager, f"{PY} -c \"import sys; sys.exit(2)\"")
    status = manager.build_status()
    assert status["notes_count"] == 2
    assert status["commands_count"] == 2
    assert status["failed_commands_count"] == 1


def test_start_while_active_raises(nogit_paths):
    manager = SessionManager(nogit_paths)
    manager.start("one")
    with pytest.raises(SessionError):
        manager.start("two")


def test_note_without_active_raises(nogit_paths):
    manager = SessionManager(nogit_paths)
    with pytest.raises(SessionError):
        manager.add_note("orphan note")


def test_run_without_active_raises(nogit_paths):
    manager = SessionManager(nogit_paths)
    with pytest.raises(SessionError):
        manager.require_active("run a command")


def test_end_without_active_raises(nogit_paths):
    manager = SessionManager(nogit_paths)
    with pytest.raises(SessionError):
        manager.end("pr")


def test_empty_title_and_note_rejected(nogit_paths):
    manager = SessionManager(nogit_paths)
    with pytest.raises(SessionError):
        manager.start("   ")
    manager.start("real")
    with pytest.raises(SessionError):
        manager.add_note("   ")


def test_serialization_round_trip(nogit_paths):
    manager = SessionManager(nogit_paths)
    session = manager.start("round trip")
    manager.add_note("note text")
    _record_run(manager, f"{PY} -c \"print('x')\"")

    raw = read_json(nogit_paths.session_file(session.session_id))
    rebuilt = Session.from_dict(raw)
    assert rebuilt.to_dict() == raw
    assert rebuilt.title == "round trip"
    assert len(rebuilt.command_events()) == 1
    assert len(rebuilt.note_events()) == 1


def test_interrupted_session_recovery(nogit_paths):
    manager = SessionManager(nogit_paths)
    session = manager.start("will be interrupted")

    # Simulate a crash: the underlying session file disappears, pointer remains.
    nogit_paths.session_file(session.session_id).unlink()

    status = manager.build_status()
    assert status["active"] is True
    assert status["interrupted"] is True

    with pytest.raises(SessionError):
        manager.load_active()


def test_end_records_final_git_state(git_paths):
    manager = SessionManager(git_paths)
    manager.start("git session")
    completed = manager.end("handoff")
    assert completed.git.is_repo is True
    assert completed.git.final_sha is not None
    assert completed.git.initial_sha is not None
    assert completed.git.branch == "main"


def test_modified_files_captured_on_end(git_paths):
    manager = SessionManager(git_paths)
    manager.start("with changes")
    (git_paths.project_root / "seed.txt").write_text("seed\nchanged\n", encoding="utf-8")
    completed = manager.end("pr")
    assert "seed.txt" in completed.summary.modified_files
    assert completed.summary.lines_added >= 1


def test_no_active_status(nogit_paths):
    manager = SessionManager(nogit_paths)
    status = manager.build_status()
    assert status == {"active": False}
