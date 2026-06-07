"""Tests for the doctor health-check command."""

from __future__ import annotations

from debugbrief.doctor import (
    EXIT_BLOCKED,
    EXIT_READY,
    EXIT_WARN,
    FAIL,
    PASS,
    WARN,
    run_doctor,
)
from debugbrief.session_manager import SessionManager


def _find(checks, name):
    for c in checks:
        if c.name == name:
            return c
    raise AssertionError(f"No check named {name!r}")


def test_doctor_ready_state(git_paths):
    # --fix creates dirs and adds the local ignore entry -> fully ready.
    report = run_doctor(git_paths, fix=True)
    assert report.exit_code == EXIT_READY
    assert report.summary == "DebugBrief is ready."
    assert all(c.level == PASS for c in report.checks), [
        (c.level, c.name) for c in report.checks if c.level != PASS
    ]


def test_doctor_outside_git_is_warning(nogit_paths):
    report = run_doctor(nogit_paths, fix=True)
    assert report.exit_code == EXIT_WARN
    git_check = _find(report.checks, "Git repository")
    assert git_check.level == WARN
    local_ignore = _find(report.checks, "Local ignore")
    assert local_ignore.level == PASS  # N/A outside git


def test_doctor_with_active_session(git_paths):
    manager = SessionManager(git_paths)
    manager.start("doctor session")
    report = run_doctor(git_paths)

    assert _find(report.checks, "Active session").level == PASS
    assert _find(report.checks, "Active session JSON").level == PASS
    assert _find(report.checks, "Session integrity").level == PASS
    assert _find(report.checks, "Session project root").level == PASS


def test_doctor_invalid_active_session_json(git_paths):
    git_paths.ensure_directories()
    git_paths.active_session_file.write_text("{not valid json", encoding="utf-8")
    report = run_doctor(git_paths)
    assert report.exit_code == EXIT_BLOCKED
    assert _find(report.checks, "Active session JSON").level == FAIL


def test_doctor_interrupted_session_warns(git_paths):
    manager = SessionManager(git_paths)
    session = manager.start("interrupt me")
    git_paths.session_file(session.session_id).unlink()
    report = run_doctor(git_paths)
    integrity = _find(report.checks, "Session integrity")
    assert integrity.level == WARN
    assert "interrupted" in integrity.detail.lower()


def test_doctor_session_root_mismatch_warns(git_paths):
    manager = SessionManager(git_paths)
    session = manager.start("mismatch")
    # Rewrite the persisted session to claim a different project root.
    data = session.to_dict()
    data["project_root"] = "/somewhere/else/entirely"
    from debugbrief.utils import atomic_write_json

    atomic_write_json(git_paths.session_file(session.session_id), data)
    report = run_doctor(git_paths)
    assert _find(report.checks, "Session project root").level == WARN


def test_doctor_return_codes_distinct():
    assert EXIT_READY == 0
    assert EXIT_WARN == 1
    assert EXIT_BLOCKED == 2


def test_doctor_fix_creates_dirs_and_ignore(git_paths):
    assert not git_paths.base_dir.exists()
    run_doctor(git_paths, fix=True)
    assert git_paths.base_dir.is_dir()
    assert git_paths.reports_dir.is_dir()
    exclude = git_paths.repo_root / ".git" / "info" / "exclude"
    assert ".debugbrief/" in exclude.read_text(encoding="utf-8")
