"""Tests for the `list` and `show` commands and the sessions index."""

from __future__ import annotations

import argparse
import json

import pytest

from debugbrief import cli, sessions_index
from debugbrief.models import (
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_PASSED,
    CommandClassification,
    CommandData,
    Event,
    Session,
    SessionStatus,
    Timestamps,
)
from debugbrief.paths import ProjectPaths
from debugbrief.utils import atomic_write_json


@pytest.fixture
def paths(tmp_path):
    return ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)


@pytest.fixture(autouse=True)
def _patch_resolve(monkeypatch, paths):
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    return paths


def _make_session(paths, session_id, title, start, status=SessionStatus.COMPLETED.value):
    session = Session(
        title=title,
        project_root=str(paths.project_root),
        session_id=session_id,
        status=status,
        timestamps=Timestamps(start=start, end=start),
    )
    session.events.append(Event.note("a note", start))
    data = CommandData(
        command="python -m pytest",
        started_at=start,
        ended_at=start,
        duration_seconds=0.1,
        exit_code=0,
        classification=CommandClassification(
            is_test=True, is_verification=True, tool="pytest", status=COMMAND_STATUS_PASSED
        ),
    )
    session.events.append(Event.command(data, start))
    session.summary.notes_count = 1
    session.summary.commands_count = 1
    paths.ensure_directories()
    atomic_write_json(paths.session_file(session_id), session.to_dict())
    return session


# list ---------------------------------------------------------------------
def test_list_no_sessions(paths, capsys):
    rc = cli.cmd_list(argparse.Namespace(json=False))
    assert rc == 1
    assert "No DebugBrief sessions found" in capsys.readouterr().err


def test_list_multiple_reverse_chronological(paths, capsys):
    _make_session(paths, "aaaaaaaa-0000-4000-8000-000000000001", "Older", "2026-01-01T00:00:00.000Z")
    _make_session(paths, "bbbbbbbb-0000-4000-8000-000000000002", "Newer", "2026-02-01T00:00:00.000Z")
    rc = cli.cmd_list(argparse.Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.index("Newer") < out.index("Older")
    assert "verification: verified" in out


def test_list_json(paths, capsys):
    _make_session(paths, "aaaaaaaa-0000-4000-8000-000000000001", "Only", "2026-01-01T00:00:00.000Z")
    rc = cli.cmd_list(argparse.Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["title"] == "Only"
    assert payload[0]["short_id"] == "aaaaaaaa"
    assert payload[0]["verified"] is True
    assert "report_modes" in payload[0]


def test_list_normalizes_malformed_stored_command_counts(paths, capsys):
    session = _make_session(
        paths,
        "aaaaaaaa-0000-4000-8000-000000000001",
        "Malformed counts",
        "2026-01-01T00:00:00.000Z",
    )
    session.events.append(
        Event(
            type="command",
            timestamp="2026-01-01T00:00:01.000Z",
            data={
                "command": "pytest --version",
                "classification": {"status": "passed"},
                "exit_code": 1,
            },
        )
    )
    session.summary.commands_count = 0
    session.summary.failed_commands_count = 0
    atomic_write_json(paths.session_file(session.session_id), session.to_dict())
    assert cli.cmd_list(argparse.Namespace(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["commands_count"] == 2
    assert payload[0]["failed_commands_count"] == 1


def test_show_json_normalizes_malformed_stored_command_counts(paths, capsys):
    session = _make_session(
        paths,
        "aaaaaaaa-0000-4000-8000-000000000001",
        "Malformed show counts",
        "2026-01-01T00:00:00.000Z",
    )
    session.events.append(
        Event(
            type="command",
            timestamp="2026-01-01T00:00:01.000Z",
            data={
                "command": "pytest --version",
                "classification": {"status": "passed"},
                "exit_code": 1,
            },
        )
    )
    session.summary.commands_count = 0
    session.summary.failed_commands_count = 0
    atomic_write_json(paths.session_file(session.session_id), session.to_dict())
    assert cli.cmd_show(
        argparse.Namespace(session_id=session.session_id, json=True)
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["commands_count"] == 2
    assert payload["summary"]["failed_commands_count"] == 1


def test_show_json_canonicalizes_nonfinite_command_data(paths, capsys):
    session = _make_session(
        paths,
        "aaaaaaaa-0000-4000-8000-000000000001",
        "Nonfinite command",
        "2026-01-01T00:00:00.000Z",
    )
    session.events[-1].data["duration_seconds"] = float("nan")
    session.events.append(
        Event(type="snapshot", timestamp="2026-01-01T00:00:02.000Z", data={"value": float("nan")})
    )
    atomic_write_json(paths.session_file(session.session_id), session.to_dict())
    assert cli.cmd_show(
        argparse.Namespace(session_id=session.session_id, json=True)
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["events"][-2]["data"]["duration_seconds"] == 0.0
    assert payload["events"][-1]["data"]["value"] is None


def test_list_json_empty(paths, capsys):
    rc = cli.cmd_list(argparse.Namespace(json=True))
    assert rc == 1
    assert capsys.readouterr().out.strip() == "[]"


def test_is_verified_uses_latest_check_outcome(paths):
    session = _make_session(
        paths,
        "aaaaaaaa-0000-4000-8000-000000000001",
        "Later failure",
        "2026-01-01T00:00:00.000Z",
    )
    session.events.append(
        Event.command(
            CommandData(
                command="python -m pytest",
                started_at="2026-01-01T00:00:02.000Z",
                ended_at="2026-01-01T00:00:03.000Z",
                duration_seconds=0.1,
                exit_code=1,
                classification=CommandClassification(
                    is_test=True, tool="pytest", status=COMMAND_STATUS_FAILED
                ),
            ),
            "2026-01-01T00:00:03.000Z",
        )
    )
    assert sessions_index.is_verified(session) is False


# show ---------------------------------------------------------------------
def test_show_full_id(paths, capsys):
    sid = "aaaaaaaa-0000-4000-8000-000000000001"
    _make_session(paths, sid, "Full id session", "2026-01-01T00:00:00.000Z")
    rc = cli.cmd_show(argparse.Namespace(session_id=sid, json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Full id session" in out
    assert sid in out
    assert "python -m pytest" in out


def test_show_short_unambiguous_prefix(paths, capsys):
    _make_session(paths, "abc11111-0000-4000-8000-000000000001", "First", "2026-01-01T00:00:00.000Z")
    _make_session(paths, "xyz22222-0000-4000-8000-000000000002", "Second", "2026-02-01T00:00:00.000Z")
    rc = cli.cmd_show(argparse.Namespace(session_id="abc1", json=False))
    assert rc == 0
    assert "First" in capsys.readouterr().out


def test_show_ambiguous_prefix(paths, capsys):
    _make_session(paths, "abc11111-0000-4000-8000-000000000001", "First", "2026-01-01T00:00:00.000Z")
    _make_session(paths, "abc22222-0000-4000-8000-000000000002", "Second", "2026-02-01T00:00:00.000Z")
    rc = cli.cmd_show(argparse.Namespace(session_id="abc", json=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "Ambiguous" in err


def test_show_missing_id(paths, capsys):
    _make_session(paths, "abc11111-0000-4000-8000-000000000001", "First", "2026-01-01T00:00:00.000Z")
    rc = cli.cmd_show(argparse.Namespace(session_id="zzzz", json=False))
    assert rc == 1
    assert "No session found" in capsys.readouterr().err


def test_show_json(paths, capsys):
    sid = "aaaaaaaa-0000-4000-8000-000000000001"
    _make_session(paths, sid, "JSON session", "2026-01-01T00:00:00.000Z")
    rc = cli.cmd_show(argparse.Namespace(session_id="aaaa", json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == sid
    assert payload["title"] == "JSON session"
    assert "events" in payload


# sessions_index -----------------------------------------------------------
def test_load_all_sessions_requires_body_id_to_match_filename(paths):
    filename_id = "aaaaaaaa-0000-4000-8000-000000000001"
    body_id = "bbbbbbbb-0000-4000-8000-000000000002"
    session = _make_session(paths, body_id, "Wrong binding", "2026-01-01T00:00:00.000Z")
    paths.ensure_directories()
    paths.session_file(filename_id).write_text(
        json.dumps(session.to_dict()), encoding="utf-8"
    )
    paths.session_file(body_id).unlink()
    assert sessions_index.load_all_sessions(paths) == []
    assert sessions_index.report_modes_for(paths, "../../outside") == []


def test_resolve_session_id(paths):
    _make_session(paths, "abc11111-0000-4000-8000-000000000001", "First", "2026-01-01T00:00:00.000Z")
    _make_session(paths, "abc22222-0000-4000-8000-000000000002", "Second", "2026-02-01T00:00:00.000Z")
    resolved, matches = sessions_index.resolve_session_id(paths, "abc1")
    assert resolved == "abc11111-0000-4000-8000-000000000001"
    resolved, matches = sessions_index.resolve_session_id(paths, "abc")
    assert resolved is None
    assert len(matches) == 2
    resolved, matches = sessions_index.resolve_session_id(paths, "nope")
    assert resolved is None
    assert matches == []
