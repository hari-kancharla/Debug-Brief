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
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from debugbrief.paths import ProjectPaths, UnsafeStateDirectory
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


def test_recover_does_not_warn_when_the_command_was_already_recorded(manager, tmp_path):
    from debugbrief.command_runner import run_command

    session = manager.start("t")
    with manager.command_lease("echo hi", str(tmp_path)) as command_id:
        result = run_command("echo hi", cwd=tmp_path, echo=False)
        manager.record_command(result, command_id=command_id)
    # Simulate a crash AFTER recording but BEFORE clearing the lease: the event is
    # present, and a stale lease for that same command_id remains (lock now free).
    reloaded = manager.load_session_file(session.session_id)
    manager._write_command_lease(reloaded, command_id, "echo hi", str(tmp_path))
    assert manager.paths.active_command_file.exists()

    assert manager.recover()["lease"] == "cleared_stale"
    final = manager.load_session_file(session.session_id)
    assert len(final.command_events()) == 1
    # The result was recorded, so recovery must NOT claim the command was lost.
    assert not any("did not finish" in w for w in final.warnings)


def test_recover_reports_an_unclearable_directory_lease(manager, tmp_path):
    manager.start("t")
    # A non-empty directory at the lease path can be neither unlinked nor rmdir'd.
    manager.paths.active_command_file.mkdir()
    (manager.paths.active_command_file / "junk").write_text("x", encoding="utf-8")

    result = manager.recover()
    assert result["lease"] == "unclearable"  # not falsely claimed cleared
    assert manager.paths.active_command_file.is_dir()  # still there
    # A subsequent run refuses cleanly instead of crashing in atomic_write_json.
    with pytest.raises(UnsafeStateDirectory), manager.command_lease("x", str(tmp_path)):
        pass


def test_result_is_recorded_against_the_lease_session(manager, tmp_path):
    from debugbrief.command_runner import run_command
    from debugbrief.models import Session

    s1 = manager.start("first")
    with manager.command_lease("echo hi", str(tmp_path)) as command_id:
        result = run_command("echo hi", cwd=tmp_path, echo=False)
        # The active pointer moves to a different session while the command runs.
        s2 = Session(title="second", project_root=str(tmp_path))
        s2.timestamps.start = "2026-01-01T00:00:00.000Z"
        manager.save_session(s2)
        manager._write_active_pointer(s2)
        manager.record_command(result, command_id=command_id)
    # The result lands in the session that owned the lease (s1), never in s2.
    assert len(manager.load_session_file(s1.session_id).command_events()) == 1
    assert len(manager.load_session_file(s2.session_id).command_events()) == 0


def test_start_refuses_while_a_command_is_live(manager, tmp_path):
    manager.start("first")
    with manager.command_lease("echo hi", str(tmp_path)):
        # Even with the active pointer gone, a live command must block a new start
        # so the running command's result cannot land in the new session.
        manager._clear_active_pointer()
        with pytest.raises(SessionError, match="still running"):
            manager.start("second")


def test_recover_reaps_a_dangling_lease_symlink(manager, tmp_path):
    manager.start("t")
    # A dangling symlink at active_command.json: exists() is False, lexists True.
    manager.paths.active_command_file.symlink_to(tmp_path / "nowhere")
    manager.recover()
    assert not manager.paths.active_command_file.is_symlink()  # removed, not skipped


def test_lease_write_failure_releases_the_lock(manager, tmp_path, monkeypatch):
    manager.start("t")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_write_command_lease", boom)
    with pytest.raises(OSError), manager.command_lease("x", str(tmp_path)):
        pass
    monkeypatch.undo()
    # The lock was released despite the failure, so a new command can be leased.
    with manager.command_lease("y", str(tmp_path)) as cid:
        assert cid


def test_command_pass_fds_exposes_the_lock_during_a_lease(manager, tmp_path):
    assert manager.command_pass_fds == ()
    manager.start("t")
    with manager.command_lease("x", str(tmp_path)):
        assert len(manager.command_pass_fds) == 1  # the held lock fd, for the child
    assert manager.command_pass_fds == ()


def test_inherited_flock_survives_an_abrupt_parent_fd_close(tmp_path):
    # The fix relies on a child inheriting the command lock (via pass_fds) keeping
    # the flock held even if the parent drops its fd without unlocking (a crash).
    import fcntl
    import time

    lock = tmp_path / "x.lock"
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    child = subprocess.Popen(
        [PY, "-c", "import time; time.sleep(5)"], pass_fds=(fd,)
    )
    try:
        os.close(fd)  # parent drops its fd without LOCK_UN, like an abrupt death
        time.sleep(0.3)
        probe = os.open(str(lock), os.O_RDWR)
        try:
            # The child still holds the inherited lock, so this fails.
            with pytest.raises(OSError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_clean_exit_keeps_a_descendant_that_inherited_the_lease(manager, tmp_path):
    # A process the command backgrounds inherits the command-lock fd. Because
    # inherited fds share one flock, the clean-exit release must close the
    # parent's fd without an explicit LOCK_UN, or it would free the descendant's
    # lock too. Simulate the descendant with os.dup (a shared open description).
    manager.start("t")
    with manager.command_lease("x", str(tmp_path)):
        descendant_fd = os.dup(manager._command_lock_fd)
    try:
        # The lease context exited cleanly and closed the parent's fd, but the
        # inherited copy still holds the lock, so the lease is not free yet.
        assert manager._command_is_active() is True
    finally:
        os.close(descendant_fd)
    # Once the descendant releases (exits), the lease is free again.
    assert manager._command_is_active() is False


def test_recover_and_status_report_a_background_held_lock(manager):
    # After a command's lease metadata is cleared, a backgrounded descendant can
    # still hold the inherited command lock. recover and status must explain this
    # lock-only state rather than looking healthy, since run/end/cancel stay
    # blocked until the descendant exits. Simulate the descendant by holding the
    # command lock with no active_command.json present.
    import fcntl

    manager.start("t")
    fd = manager._open_command_lock()
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert manager.build_status().get("background_lock") is True
        assert manager.recover()["lease"] == "held_by_background"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # Once the descendant releases the lock, status no longer flags it.
    assert manager.build_status().get("background_lock") is False


def test_recover_creates_no_state_when_nothing_exists(manager):
    # recover must stay read-only on a fresh project: it must not create
    # .debugbrief/ (which the repo lock would otherwise do under the umask).
    assert manager.recover()["action"] == "none"
    assert not manager.paths.base_dir.exists()


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


def test_failed_persistence_leaves_the_lease_for_recovery(manager, tmp_path, monkeypatch):
    from debugbrief.command_runner import run_command

    manager.start("t")

    def boom(_session):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "save_session", boom)
    with pytest.raises(OSError), manager.command_lease("echo hi", str(tmp_path)) as cid:
        result = run_command("echo hi", cwd=tmp_path, echo=False)
        manager.record_command(result, command_id=cid)
    # Persistence failed, so the lease must remain for recover, not be erased.
    assert manager.paths.active_command_file.exists()

    # A fresh (unpatched) manager recovers it: the command never persisted, so it
    # is reported as a lost command.
    sm2 = SessionManager(manager.paths)
    assert sm2.recover()["lease"] == "cleared_stale"
    session = sm2.load_active()
    assert session is not None and any("did not finish" in w for w in session.warnings)


def test_new_run_reaps_a_stale_lease_before_starting(manager, tmp_path):
    from debugbrief.command_runner import run_command

    session = manager.start("t")
    manager._write_command_lease(session, "ghost", "pytest --crashed", str(tmp_path))
    with manager.command_lease("echo hi", str(tmp_path)) as command_id:
        result = run_command("echo hi", cwd=tmp_path, echo=False)
        manager.record_command(result, command_id=command_id)
    final = manager.load_active()
    assert any("did not finish" in w for w in final.warnings)  # stale one reported
    assert len(final.command_events()) == 1  # the new command recorded


def test_end_reaps_a_stale_lease_before_finalizing(manager, tmp_path):
    session = manager.start("t")
    manager._write_command_lease(session, "ghost", "pytest --crashed", str(tmp_path))
    ended = manager.end("pr")
    assert ended.status == "COMPLETED"
    assert any("did not finish" in w for w in ended.warnings)


def test_cancel_reaps_a_stale_lease(manager, tmp_path):
    session = manager.start("t")
    manager._write_command_lease(session, "ghost", "pytest --crashed", str(tmp_path))
    manager.cancel()
    reloaded = manager.load_session_file(session.session_id)
    assert reloaded.status == "ABANDONED"
    assert any("did not finish" in w for w in reloaded.warnings)


def test_end_does_not_warn_when_leftover_lease_was_already_recorded(manager, tmp_path):
    from debugbrief.command_runner import run_command

    manager.start("t")
    with manager.command_lease("echo hi", str(tmp_path)) as command_id:
        result = run_command("echo hi", cwd=tmp_path, echo=False)
        manager.record_command(result, command_id=command_id)
    # Leftover metadata for the SAME, already-recorded command (process died after
    # recording, before clearing). end must reap it without a false warning.
    manager._write_command_lease(manager.load_active(), command_id, "echo hi", str(tmp_path))
    ended = manager.end("pr")
    assert ended.status == "COMPLETED"
    assert not any("did not finish" in w for w in ended.warnings)


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
