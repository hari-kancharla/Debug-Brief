"""Trust-boundary tests for persisted state.

Session and lease state lives inside the project (``.debugbrief/``), so it can be
hand-edited, corrupted, or shipped by a cloned repository. These tests pin the
guarantees that keep that untrusted input safe:

- the id embedded in a session file cannot escape ``sessions/`` (it is bound to
  the validated filename, and the write boundary refuses an invalid id);
- malformed state produces a controlled ``SessionError``, never a raw traceback;
- a command stored in Git-tracked state is never re-executed by ``redo``;
- lock files are opened with the same symlink/FIFO protection as state reads;
- a dangling-symlink lease that cannot be removed is reported, not hidden.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from debugbrief import cli, git_utils
from debugbrief.models import CommandData, Event, Session
from debugbrief.paths import ProjectPaths, UnsafeStateDirectory
from debugbrief.session_manager import SessionError, SessionManager
from debugbrief.utils import now_iso8601

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX symlink/FIFO/flock; project is Unix-only"
)


@pytest.fixture
def project(tmp_path):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    return paths, SessionManager(paths)


def _git(args, cwd):
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "a@b.c"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    _git(["checkout", "-q", "-b", "main"], tmp_path)


# Blocker 1: the id inside a session file cannot become a write path ----------
def test_load_session_file_refuses_a_path_traversing_embedded_id(project):
    paths, mgr = project
    session = mgr.start("real")
    path = paths.session_file(session.session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    # The filename is a valid uuid, but the body claims a traversing id.
    data["session_id"] = "../../../../tmp/debugbrief-evil"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SessionError, match="declares a different id"):
        mgr.load_session_file(session.session_id)


def test_load_session_file_refuses_a_different_valid_id(project):
    paths, mgr = project
    session = mgr.start("real")
    path = paths.session_file(session.session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["session_id"] = uuid.uuid4().hex  # valid, but not this file's id
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SessionError, match="declares a different id"):
        mgr.load_session_file(session.session_id)


def test_a_tampered_active_session_id_cannot_write_outside_sessions(project):
    paths, mgr = project
    session = mgr.start("real")
    path = paths.session_file(session.session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["session_id"] = "../evil"  # would land at .debugbrief/evil.json
    path.write_text(json.dumps(data), encoding="utf-8")
    traversal_target = paths.sessions_dir.parent / "evil.json"

    with pytest.raises(SessionError):
        mgr.add_note("hello")
    assert not traversal_target.exists()  # the out-of-directory write never happened


def test_save_session_refuses_an_invalid_id(project):
    paths, mgr = project
    bad = Session(
        title="t", project_root=str(paths.project_root), session_id="../evil"
    )
    with pytest.raises(SessionError, match="invalid id"):
        mgr.save_session(bad)
    assert not (paths.sessions_dir.parent / "evil.json").exists()


# Malformed state degrades to a controlled error, not a traceback -------------
def _write_session_raw(paths, session_id, body):
    paths.sessions_dir.mkdir(parents=True, exist_ok=True)
    text = body if isinstance(body, str) else json.dumps(body)
    (paths.sessions_dir / f"{session_id}.json").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "body",
    [
        "[1, 2, 3]",  # a JSON array, not an object
        "null",  # JSON null
        '"a string"',  # a JSON string
    ],
)
def test_load_session_file_rejects_a_non_object_file(project, body):
    paths, mgr = project
    sid = uuid.uuid4().hex
    _write_session_raw(paths, sid, body)
    with pytest.raises(SessionError):  # controlled, not a raw TypeError/AttributeError
        mgr.load_session_file(sid)


@pytest.mark.parametrize(
    "field,value",
    [
        ("warnings", None),
        ("events", None),
        ("warnings", "not-a-list"),
    ],
)
def test_load_session_file_rejects_malformed_collection_fields(project, field, value):
    paths, mgr = project
    sid = uuid.uuid4().hex
    body = {
        "session_id": sid,
        "title": "t",
        "project_root": "/x",
        "warnings": [],
        "events": [],
    }
    body[field] = value
    _write_session_raw(paths, sid, body)
    with pytest.raises(SessionError):
        mgr.load_session_file(sid)


def test_command_data_coerces_a_bad_numeric_leaf(project):
    # A command's leaf values (e.g. a non-numeric duration) are parsed lazily by
    # CommandData.from_dict, not at load. A bad value degrades to a default so a
    # consumer reading the record never crashes, while the session still loads.
    paths, mgr = project
    sid = uuid.uuid4().hex
    body = {
        "session_id": sid,
        "title": "t",
        "project_root": "/x",
        "warnings": [],
        "events": [
            {
                "type": "command",
                "timestamp": "2026-01-01T00:00:00Z",
                "data": {
                    "command": "pytest",
                    "started_at": "",
                    "ended_at": "",
                    "duration_seconds": "not-a-number",
                    "exit_code": 0,
                },
            }
        ],
    }
    _write_session_raw(paths, sid, body)
    loaded = mgr.load_session_file(sid)  # loads without raising
    command = CommandData.from_dict(loaded.command_events()[0].data)
    assert command.duration_seconds == 0.0  # bad value degraded, no crash


def test_a_null_nested_object_degrades_to_defaults(project):
    # A null git/timestamps block is cosmetic state; it must load with defaults
    # rather than raise, since it can be safely recomputed.
    paths, mgr = project
    sid = uuid.uuid4().hex
    body = {
        "session_id": sid,
        "title": "t",
        "project_root": "/x",
        "warnings": [],
        "events": [],
        "git": None,
        "timestamps": None,
        "summary": None,
    }
    _write_session_raw(paths, sid, body)
    loaded = mgr.load_session_file(sid)
    assert loaded.session_id == sid
    assert loaded.git.is_repo is False
    assert loaded.timestamps.start is None


# Blocker 2: a Git-tracked command is never re-executed by redo ---------------
def test_is_tracked_distinguishes_committed_from_untracked(tmp_path):
    _init_repo(tmp_path)
    tracked = tmp_path / "a.txt"
    tracked.write_text("x", encoding="utf-8")
    _git(["add", "a.txt"], tmp_path)
    _git(["commit", "-q", "-m", "x"], tmp_path)
    untracked = tmp_path / "b.txt"
    untracked.write_text("y", encoding="utf-8")

    assert git_utils.is_tracked(tmp_path, tracked) is True
    assert git_utils.is_tracked(tmp_path, untracked) is False
    # Outside a repo, conservatively False.
    assert git_utils.is_tracked(tmp_path / "nope", tmp_path / "nope" / "c") is False


def test_redo_refuses_a_command_from_git_tracked_state(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    paths = ProjectPaths(
        project_root=tmp_path, is_git_repo=True, repo_root=str(tmp_path)
    )
    mgr = SessionManager(paths)
    session = mgr.start("seeded")
    now = now_iso8601()
    command = CommandData(
        command="echo seeded-and-dangerous",
        started_at=now,
        ended_at=now,
        duration_seconds=0.0,
        exit_code=0,
    )
    session.events.append(Event.command(command, now))
    mgr.save_session(session)
    # Commit the state, as a cloned repository would ship it (force past the
    # local exclude DebugBrief writes).
    _git(["add", "-f", ".debugbrief"], tmp_path)
    _git(["commit", "-q", "-m", "seed"], tmp_path)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)

    assert cli.main(["redo"]) == 1
    err = capsys.readouterr().err
    assert "tracked by Git" in err


def test_redo_runs_normally_for_untracked_state(tmp_path, monkeypatch, capsys):
    # The same setup but without committing the state must not be blocked.
    _init_repo(tmp_path)
    paths = ProjectPaths(
        project_root=tmp_path, is_git_repo=True, repo_root=str(tmp_path)
    )
    mgr = SessionManager(paths)
    session = mgr.start("local")
    now = now_iso8601()
    command = CommandData(
        command="true", started_at=now, ended_at=now, duration_seconds=0.0, exit_code=0
    )
    session.events.append(Event.command(command, now))
    mgr.save_session(session)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)

    # redo re-runs `true`, which exits 0; the point is it is not refused.
    assert cli.main(["redo"]) == 0
    err = capsys.readouterr().err
    assert "tracked by Git" not in err


# Lock files get the same symlink/FIFO protection as state reads --------------
def test_command_lock_rejects_a_symlinked_lock_path(project):
    paths, mgr = project
    mgr.start("t")
    paths.base_dir.mkdir(parents=True, exist_ok=True)
    target = paths.base_dir / "elsewhere"
    target.write_text("", encoding="utf-8")
    paths.command_lock_file.symlink_to(target)
    with pytest.raises(UnsafeStateDirectory), mgr.command_lease(
        "pytest", str(paths.project_root)
    ):
        pass


def test_command_lock_rejects_a_fifo_lock_path(project):
    paths, mgr = project
    mgr.start("t")
    paths.base_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(paths.command_lock_file)
    with pytest.raises(UnsafeStateDirectory), mgr.command_lease(
        "pytest", str(paths.project_root)
    ):
        pass


# A lease that cannot be removed is reported, not silently "cleared" ----------
def test_clear_command_lease_reports_a_stuck_dangling_symlink(project, monkeypatch):
    paths, mgr = project
    paths.base_dir.mkdir(parents=True, exist_ok=True)
    # A dangling symlink: exists() is False, but the entry is still present.
    paths.active_command_file.symlink_to(paths.base_dir / "missing-target")
    # Simulate removal failing (e.g. a read-only parent dir): make unlink/rmdir
    # no-ops so the entry remains after the attempt.
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: None)
    monkeypatch.setattr(os, "rmdir", lambda *a, **k: None)

    # lexists() catches the lingering symlink that exists() would miss, so the
    # cleanup must not claim success.
    assert mgr._clear_command_lease() is False
