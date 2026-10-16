"""Real recorder deaths: verify cleanup before any emergency test cleanup."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from debugbrief.command_runner import run_command


def _running(pid):
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
    )
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@pytest.mark.parametrize("pipes", [False, True])
@pytest.mark.parametrize("ignore_term", [False, True])
def test_sigkill_recorder_cleans_group_and_releases_lease(tmp_path, pipes, ignore_term):
    """No timeout, no output dependence, and no harness cleanup before assertions."""
    cli = [sys.executable, "-m", "debugbrief"]
    subprocess.run([*cli, "start", "Hard crash"], cwd=tmp_path, check=True,
                   stdout=subprocess.DEVNULL)
    pids = tmp_path / "pids.json"
    pending_pids = tmp_path / "pids.pending"
    marker = tmp_path / "survived"
    child_code = (
        "import signal,time; from pathlib import Path; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
        + f"time.sleep(2); Path({str(marker)!r}).write_text('survived'); time.sleep(30)"
    )
    command = (
        "import os,sys,subprocess,json,time,signal; from pathlib import Path; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
        + f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}], "
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True); "
        f"Path({str(pending_pids)!r}).write_text(json.dumps([os.getpid(),child.pid,os.getppid()])); "
        f"os.replace({str(pending_pids)!r},{str(pids)!r}); "
        "time.sleep(30)"
    )
    args = ["run", "--verify", "--", sys.executable, "-c", command]
    if pipes:
        # Exercise the real CLI with its PTY allocator deliberately unavailable.
        entry = (
            "import pty; pty.openpty=lambda: (_ for _ in ()).throw(OSError('no pty')); "
            "from debugbrief.cli import main; raise SystemExit(main())"
        )
        cli = [sys.executable, "-c", entry]
    recorder = subprocess.Popen([*cli, *args], cwd=tmp_path,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    identities = []
    try:
        deadline = time.monotonic() + 10
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pids.exists(), "command never started"
        identities = json.loads(pids.read_text())
        # Another process group must remain untouched by cleanup.
        assert os.getpgid(identities[0]) == identities[2]
        recorder.kill()
        recorder.communicate(timeout=5)
        deadline = time.monotonic() + 5
        while any(_running(pid) for pid in identities) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(_running(pid) for pid in identities), "owned processes survived SIGKILL"
        time.sleep(2.1)
        assert not marker.exists(), "descendant performed work after recorder death"
        recovered = subprocess.run(
            [sys.executable, "-m", "debugbrief", "recover"], cwd=tmp_path,
            capture_output=True, text=True, timeout=5,
        )
        assert recovered.returncode == 0, recovered.stderr
        assert not (tmp_path / ".debugbrief/active_command.json").exists()
        session = json.loads(next((tmp_path / ".debugbrief/sessions").glob("*.json")).read_text())
        assert not [event for event in session["events"] if event["type"] == "command"]
        assert any("did not finish" in warning for warning in session["warnings"])
        # The next capture must work immediately; recovery never invents a pass.
        result = subprocess.run(
            [sys.executable, "-m", "debugbrief", "run", "--", sys.executable,
             "-c", "print('after recovery')"], cwd=tmp_path,
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, result.stderr
    finally:
        if recorder.poll() is None:
            recorder.kill()
            recorder.communicate(timeout=5)
        # Emergency cleanup happens after the assertions and cannot make a pass.
        if identities and _running(identities[2]):
            os.killpg(identities[2], signal.SIGKILL)


def test_normal_completion_reaps_guard_and_term_resistant_background(tmp_path):
    pids = tmp_path / "pids.json"
    code = (
        "import subprocess,os,sys,json,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)']); "
        "time.sleep(.1); "
        f"Path({str(pids)!r}).write_text(json.dumps([os.getppid(),child.pid]))"
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
    try:
        result = run_command(f"{sys.executable} -c {__import__('shlex').quote(code)}",
                             cwd=tmp_path, echo=False)
        assert result.command_data.exit_code == 0
        assert result.warning and "background process" in result.warning
        assert not any(_running(pid) for pid in json.loads(pids.read_text()))
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_parent_dies_during_supervisor_startup(tmp_path):
    """EOF before the startup config is complete must never launch a command."""
    from debugbrief import _process_guard

    read_fd, write_fd = os.pipe()
    status_r, status_w = os.pipe()
    marker = tmp_path / "unexpected"
    guard = subprocess.Popen(
        [sys.executable, "-I", "-S", _process_guard.__file__, str(read_fd), str(status_w)],
        pass_fds=(read_fd, status_w), start_new_session=True,
    )
    os.close(read_fd)
    os.close(status_w)
    try:
        os.write(write_fd, json.dumps({"args": ["touch", str(marker)]}).encode()[:15])
    finally:
        os.close(write_fd)
    assert guard.wait(timeout=5) == 0
    assert os.read(status_r, 4096) == b""
    os.close(status_r)
    assert not marker.exists()


def test_exhausted_descriptors_do_not_leak_the_lifetime_pipe(tmp_path, monkeypatch):
    from debugbrief import _process_guard

    real_pipe = os.pipe
    opened = []

    def limited_pipe():
        if opened:
            raise OSError("too many open files")
        pair = real_pipe()
        opened.extend(pair)
        return pair

    monkeypatch.setattr(_process_guard.os, "pipe", limited_pipe)
    with pytest.raises(OSError, match="too many open files"):
        _process_guard.SupervisedProcess(["true"], tmp_path, False, None, None)
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
