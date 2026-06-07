"""Snapshot tests for the derived report across modes and scenarios.

Reports are generated from deterministic, fully in-memory fake session data.
Dynamic fields (UUIDs, timestamps, Git SHAs, and the absolute project path) are
normalized to stable placeholders before comparison, so snapshots are not
brittle across machines or runs.

The fixtures deliberately exercise the new derived report:

- a real red-to-green transition with per-event git snapshots,
- a notes-only / no-test session, and
- a failing-command session with a captured error.

To regenerate snapshots intentionally:

    DEBUGBRIEF_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from debugbrief.models import (
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_PASSED,
    CommandClassification,
    CommandData,
    Event,
    FileChange,
    GitState,
    Session,
    SessionStatus,
    Summary,
    Timestamps,
)
from debugbrief.reporters import VALID_MODES, render_report

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
PROJECT_ROOT = "/abs/path/to/example-project"

# Deterministic, fixed values that look realistic and get normalized away.
_FIXED_UUID = "11111111-2222-4333-8444-555555555555"
_INITIAL_SHA = "abc123abc123abc123abc123abc123abc123abcd"
_FINAL_SHA = "def456def456def456def456def456def456defa"
_T0 = "2026-01-02T03:04:05.000Z"
_T1 = "2026-01-02T03:04:10.000Z"
_T2 = "2026-01-02T03:05:30.000Z"


def _command_event(command, status, ts, exit_code, changed_files=None, **cls_kwargs):
    stderr = cls_kwargs.pop("stderr_preview", "")
    data = CommandData(
        command=command,
        started_at=ts,
        ended_at=ts,
        duration_seconds=0.25,
        exit_code=exit_code,
        stderr_preview=stderr,
        classification=CommandClassification(status=status, **cls_kwargs),
        git_changed_files=list(changed_files or []),
        git_head="abc123a" if changed_files is not None else None,
    )
    return Event.command(data, ts)


def _red_to_green_session() -> Session:
    session = Session(
        title="Fix auth token refresh race condition",
        project_root=PROJECT_ROOT,
        session_id=_FIXED_UUID,
        status=SessionStatus.COMPLETED.value,
        git=GitState(
            is_repo=True,
            repo_root=PROJECT_ROOT,
            initial_sha=_INITIAL_SHA,
            final_sha=_FINAL_SHA,
            branch="feature/auth-refresh",
            detached_head=False,
        ),
        timestamps=Timestamps(start=_T0, end=_T2),
    )
    session.events.append(Event.snapshot({"phase": "start"}, _T0))
    session.events.append(
        Event.note("Token refresh fails when two requests retry simultaneously.", _T0)
    )
    session.events.append(
        _command_event(
            "python -m pytest tests/test_auth.py",
            COMMAND_STATUS_FAILED,
            _T0,
            1,
            changed_files=["src/auth/refresh.py"],
            is_test=True,
            tool="pytest",
            stderr_preview="AssertionError: token refreshed twice",
        )
    )
    session.events.append(
        Event.note("Refresh state is shared across concurrent requests.", _T1)
    )
    session.events.append(
        _command_event(
            "python -m pytest tests/test_auth.py",
            COMMAND_STATUS_PASSED,
            _T1,
            0,
            changed_files=["src/auth/refresh.py", "tests/test_auth.py"],
            is_test=True,
            is_verification=True,
            tool="pytest",
        )
    )
    session.events.append(
        _command_event(
            "npm run build",
            COMMAND_STATUS_PASSED,
            _T2,
            0,
            changed_files=["src/auth/refresh.py", "tests/test_auth.py"],
            tool="npm",
            is_verification=True,
        )
    )
    session.events.append(Event.snapshot({"phase": "end"}, _T2))

    session.summary = Summary(
        modified_files=["src/auth/refresh.py", "tests/test_auth.py", "docs/old.md"],
        file_changes=[
            FileChange("M", "src/auth/refresh.py"),
            FileChange("A", "tests/test_auth.py"),
            FileChange("D", "docs/old.md"),
        ],
        lines_added=42,
        lines_deleted=7,
        tests_run=["python -m pytest tests/test_auth.py"],
        notes_count=2,
        commands_count=3,
        failed_commands_count=1,
        command_capture_status="full",
    )
    return session


def _no_test_session() -> Session:
    session = Session(
        title="Look into slow startup",
        project_root=PROJECT_ROOT,
        session_id=_FIXED_UUID,
        status=SessionStatus.COMPLETED.value,
        git=GitState(
            is_repo=True,
            repo_root=PROJECT_ROOT,
            initial_sha=_INITIAL_SHA,
            final_sha=_INITIAL_SHA,
            branch="main",
        ),
        timestamps=Timestamps(start=_T0, end=_T1),
    )
    session.events.append(Event.snapshot({"phase": "start"}, _T0))
    session.events.append(Event.note("Startup feels slow on a cold cache.", _T0))
    session.events.append(
        _command_event("echo checking", COMMAND_STATUS_PASSED, _T0, 0)
    )
    session.events.append(Event.snapshot({"phase": "end"}, _T1))
    session.summary = Summary(
        notes_count=1, commands_count=1, command_capture_status="full"
    )
    return session


def _failing_session() -> Session:
    session = Session(
        title="Track down the failing migration",
        project_root=PROJECT_ROOT,
        session_id=_FIXED_UUID,
        status=SessionStatus.COMPLETED.value,
        git=GitState(
            is_repo=True,
            repo_root=PROJECT_ROOT,
            initial_sha=_INITIAL_SHA,
            final_sha=_FINAL_SHA,
            branch="fix/migration",
        ),
        timestamps=Timestamps(start=_T0, end=_T1),
    )
    session.events.append(Event.snapshot({"phase": "start"}, _T0))
    session.events.append(Event.note("Migration 014 errors on a missing column.", _T0))
    session.events.append(
        _command_event(
            "python -m pytest tests/test_migrations.py",
            COMMAND_STATUS_FAILED,
            _T1,
            1,
            changed_files=["migrations/014_add_column.py"],
            is_test=True,
            tool="pytest",
            stderr_preview=(
                "Traceback (most recent call last):\n"
                "  File \"x.py\", line 9\n"
                "OperationalError: no such column: tenant_id"
            ),
        )
    )
    session.events.append(Event.snapshot({"phase": "end"}, _T1))
    session.summary = Summary(
        modified_files=["migrations/014_add_column.py"],
        file_changes=[FileChange("M", "migrations/014_add_column.py")],
        lines_added=3,
        lines_deleted=0,
        tests_run=["python -m pytest tests/test_migrations.py"],
        notes_count=1,
        commands_count=1,
        failed_commands_count=1,
        command_capture_status="full",
    )
    return session


def normalize(text: str) -> str:
    """Replace dynamic fields with stable placeholders."""
    text = text.replace(PROJECT_ROOT, "<PROJECT_ROOT>")
    # UUIDs
    text = re.sub(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        "<UUID>",
        text,
    )
    # ISO8601 timestamps (with milliseconds + Z)
    text = re.sub(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", "<TIMESTAMP>", text
    )
    # Human-readable UTC datetimes
    text = re.sub(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC", "<DATETIME_UTC>", text
    )
    # Clock times (HH:MM:SS) -- after the longer patterns above
    text = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "<TIME>", text)
    # Git SHAs (7-40 hex) -- after UUIDs are already replaced
    text = re.sub(r"\b[0-9a-f]{7,40}\b", "<SHA>", text)
    return text


def _check_snapshot(name: str, generated: str) -> None:
    snapshot_path = SNAPSHOT_DIR / f"{name}.md"
    if os.environ.get("DEBUGBRIEF_UPDATE_SNAPSHOTS"):
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(generated, encoding="utf-8")
    assert snapshot_path.exists(), (
        f"Missing snapshot {snapshot_path}. Regenerate with "
        "DEBUGBRIEF_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py"
    )
    expected = snapshot_path.read_text(encoding="utf-8")
    assert generated == expected, (
        f"{name} report drifted from snapshot {snapshot_path}.\n"
        "If this change is intentional, regenerate with "
        "DEBUGBRIEF_UPDATE_SNAPSHOTS=1."
    )


@pytest.mark.parametrize("mode", VALID_MODES)
def test_red_to_green_snapshot(mode):
    generated = normalize(render_report(_red_to_green_session(), mode))
    _check_snapshot(f"{mode}_report", generated)


def test_no_test_pr_snapshot():
    generated = normalize(render_report(_no_test_session(), "pr"))
    _check_snapshot("no_test_pr_report", generated)


def test_failing_pr_snapshot():
    generated = normalize(render_report(_failing_session(), "pr"))
    _check_snapshot("failing_pr_report", generated)


def test_failing_incident_snapshot():
    # Incident mode surfaces the observed error verbatim from real output.
    generated = normalize(render_report(_failing_session(), "incident"))
    _check_snapshot("failing_incident_report", generated)


def test_normalize_scrubs_dynamic_fields():
    sample = (
        "id 11111111-2222-4333-8444-555555555555 at 2026-01-02T03:04:05.000Z "
        "sha abc123abc123 path /abs/path/to/example-project time 03:04:05"
    )
    out = normalize(sample)
    assert "<UUID>" in out
    assert "<TIMESTAMP>" in out
    assert "<SHA>" in out
    assert "<PROJECT_ROOT>" in out
    assert "11111111" not in out
