"""Tests for the active-command lease (#2).

A captured command holds an exclusive lock for its whole lifetime. While it runs,
no second command may start and the session may not be ended or cancelled. The
lock is released by the OS if the process dies, so a crash leaves only a stale
metadata file, which `recover` cleans without losing the session.

The in-process tests rely on flock contending across separate open() descriptions
even within one process; the subprocess tests exercise the real multiprocess
path. All are POSIX-only (flock), matching the supported platforms.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from debugbrief.paths import ProjectPaths
from debugbrief.session_manager import SessionError, SessionManager

PY = sys.executable
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")


@pytest.fixture
def manager(tmp_path):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    return SessionManager(paths)


# In-process ------------------------------------------------------------------
def _acquire_lease(manager, tmp_path, preview="ruff check ."):
    with manager.command_lease(preview, str(tmp_path)):
        pass


def test_second_command_is_rejected_while_one_is_active(manager, tmp_path):
    manager.start("t")
    with manager.command_lease("pytest -q", str(tmp_path)), \
            pytest.raises(SessionError, match="already running"):
        _acquire_lease(manager, tmp_path)


def test_live_command_blocks_end_and_cancel(manager, tmp_path):
    manager.start("t")
    with manager.command_lease("pytest -q", str(tmp_path)):
        with pytest.raises(SessionError, match="still running"):
            manager.end("pr")
        with pytest.raises(SessionError, match="still running"):
            manager.cancel()
    # Once the lease is released, ending works normally.
    session = manager.end("pr")
    assert session.status == "COMPLETED"


def test_command_recorded_once_then_end_succeeds(manager, tmp_path):
    from debugbrief.command_runner import run_command

    manager.start("t")
    with manager.command_lease("echo hi", str(tmp_path)) as command_id:
        result = run_command("echo hi", cwd=tmp_path, echo=False)
        manager.record_command(result, command_id=command_id)
        # A retried persistence with the same id must not append a duplicate.
        manager.record_command(result, command_id=command_id)
    commands = manager.load_active().command_events()
    assert len(commands) == 1
    assert manager.end("pr").status == "COMPLETED"


def test_recover_leaves_a_live_lease_untouched(manager, tmp_path):
    manager.start("t")
    with manager.command_lease("pytest -q", str(tmp_path)):
        result = manager.recover()
        assert result["lease"] == "live"
        assert manager.paths.active_command_file.exists()


def test_recover_clears_a_stale_lease_and_preserves_the_session(manager, tmp_path):
    session = manager.start("t")
    # Simulate a crashed command: lease metadata on disk, but no live lock holder.
    manager._write_command_lease(session, "cmd123", "pytest secret-arg", str(tmp_path))
    assert manager.paths.active_command_file.exists()

    result = manager.recover()
    assert result["lease"] == "cleared_stale"
    assert not manager.paths.active_command_file.exists()
    # The session itself is preserved and gains a warning about the lost command.
    reloaded = manager.load_session_file(session.session_id)
    assert reloaded.status == "ACTIVE"
    assert any("did not finish" in w for w in reloaded.warnings)


# Real multiprocess -----------------------------------------------------------
def _wait_for_lease(tmp_path: Path, timeout: float = 15.0) -> bool:
    lease = tmp_path / ".debugbrief" / "active_command.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lease.exists():
            return True
        time.sleep(0.05)
    return False


def test_two_simultaneous_starts_create_one_session(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    procs = [
        subprocess.Popen(
            [PY, "-m", "debugbrief", "start", f"race-{i}"],
            cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for i in range(5)
    ]
    codes = [p.wait(timeout=30) for p in procs]
    assert codes.count(0) == 1, f"expected exactly one start to win, got {codes}"
    sessions = list((tmp_path / ".debugbrief" / "sessions").glob("*.json"))
    assert len(sessions) == 1


def test_running_command_blocks_end_and_a_second_run(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [PY, "-m", "debugbrief", "start", "lease"],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    sleeper = subprocess.Popen(
        [PY, "-m", "debugbrief", "run", "--", PY, "-c", "import time; time.sleep(20)"],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for_lease(tmp_path), "the command never published its lease"
        # While the command runs, end and a second run are refused.
        end = subprocess.run(
            [PY, "-m", "debugbrief", "end"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert end.returncode != 0 and "still running" in end.stderr
        second = subprocess.run(
            [PY, "-m", "debugbrief", "run", "--", PY, "-c", "print('x')"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert second.returncode != 0 and "already running" in second.stderr
        # The session is still active (it was neither ended nor cancelled).
        session_file = next((tmp_path / ".debugbrief" / "sessions").glob("*.json"))
        assert json.loads(session_file.read_text())["status"] == "ACTIVE"
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=30)
