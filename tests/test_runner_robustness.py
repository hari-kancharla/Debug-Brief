"""Robustness tests for the command runner.

Covers process-tree termination, the no-hang drain for lingering descendants,
bounded retained memory, interrupt recording, and signal exit-code propagation.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from debugbrief import filters
from debugbrief.command_runner import RunResult, _BoundedText, run_command
from debugbrief.models import (
    COMMAND_STATUS_INTERRUPTED,
    CommandData,
)

PY = sys.executable


def _wait_no_orphan(marker, timeout=5.0, interval=0.1):
    """Poll until no process matching ``marker`` remains, then kill any survivor.

    The runner signals the child's process group, but the OS reaps the processes
    asynchronously, so a single check right after the parent exits can race the
    reaping under load. Poll up to ``timeout`` seconds and return the final
    ``pgrep`` output (empty once the child is gone); a non-empty result after the
    full window is a real survivor, not a timing artifact.
    """
    deadline = time.monotonic() + timeout
    while True:
        orphan = subprocess.run(
            ["pgrep", "-f", marker], stdout=subprocess.PIPE
        ).stdout.strip()
        if not orphan or time.monotonic() >= deadline:
            break
        time.sleep(interval)
    if orphan:
        subprocess.run(["pkill", "-f", marker])
    return orphan


def _result(exit_code=None, interrupted=False) -> RunResult:
    data = CommandData(
        command="x", started_at="", ended_at="", duration_seconds=0.0, exit_code=exit_code
    )
    return RunResult(
        command_data=data, timed_out=False, errored=False, interrupted=interrupted
    )


# Signal exit-code propagation (128 + N) -------------------------------------
def test_propagated_exit_code_for_signals_and_interrupt():
    assert _result(exit_code=0).propagated_exit_code == 0
    assert _result(exit_code=7).propagated_exit_code == 7
    assert _result(exit_code=-2).propagated_exit_code == 130  # SIGINT
    assert _result(exit_code=-11).propagated_exit_code == 139  # SIGSEGV
    assert _result(exit_code=-9).propagated_exit_code == 137  # SIGKILL
    assert _result(exit_code=None).propagated_exit_code == 1  # timeout/error
    assert _result(interrupted=True).propagated_exit_code == 130


def test_signal_terminated_command_propagates_128_plus_n(tmp_path):
    result = run_command("sh -c 'kill -SEGV $$'", cwd=tmp_path, echo=False)
    assert result.command_data.exit_code is not None
    assert result.command_data.exit_code < 0  # raw signal death is stored as -N
    assert result.propagated_exit_code == 139


# Interrupt classification ----------------------------------------------------
def test_classify_interrupted_is_not_verification():
    cls = filters.classify_command("pytest", exit_code=None, interrupted=True)
    assert cls.status == COMMAND_STATUS_INTERRUPTED
    assert cls.is_verification is False


# Bounded memory --------------------------------------------------------------
def test_bounded_text_retains_only_the_budget():
    bounded = _BoundedText(100)
    for _ in range(10_000):
        bounded.feed("x" * 1000)  # feed ~10 MB
    # Retained structures are bounded by the limit, not the total fed.
    assert len(bounded._prefix) <= 100
    assert len(bounded._tail) <= 100
    assert bounded.total == 10_000 * 1000
    preview, truncated = bounded.result()
    assert truncated is True
    assert "omitted" in preview
    assert preview.count("x") == 100  # head + tail keep exactly the budget


def test_bounded_text_keeps_short_input_intact():
    bounded = _BoundedText(100)
    bounded.feed("hello")
    bounded.feed(" world")
    assert bounded.result() == ("hello world", False)


def test_bounded_text_zero_limit_means_unbounded():
    bounded = _BoundedText(0)
    bounded.feed("a" * 5000)
    text, truncated = bounded.result()
    assert truncated is False
    assert len(text) == 5000


def test_run_large_output_keeps_preview_bounded(tmp_path):
    result = run_command(
        f"{PY} -c \"import sys; [sys.stdout.write('y' * 1000) for _ in range(2000)]\"",
        cwd=tmp_path,
        stdout_limit=200,
        echo=False,
    )
    preview = result.command_data.stdout_preview
    assert result.command_data.stdout_truncated is True
    assert "omitted" in preview
    assert preview.count("y") == 200


def test_multibyte_output_preserved(tmp_path):
    result = run_command(
        f"{PY} -c \"print('hello café \\u6f22\\u5b57 ok')\"", cwd=tmp_path, echo=False
    )
    assert "café 漢字 ok" in result.command_data.stdout_preview


# Process-tree termination ----------------------------------------------------
def test_timeout_kills_backgrounded_descendant(tmp_path):
    marker = f"db_robustness_marker_{tmp_path.name}"
    result = run_command(
        f"{PY} -c 'import time; time.sleep(30)' {marker} & wait",
        cwd=tmp_path,
        use_shell=True,
        timeout_seconds=1,
        echo=False,
    )
    assert result.timed_out is True
    found = _wait_no_orphan(marker)
    assert not found, "a backgrounded descendant survived the timeout"


def test_lingering_descendant_does_not_hang_the_runner(tmp_path):
    marker = f"db_linger_marker_{tmp_path.name}"
    start = time.monotonic()
    result = run_command(
        f"{PY} -c 'import time; time.sleep(3)' {marker} &",
        cwd=tmp_path,
        use_shell=True,
        echo=False,
    )
    elapsed = time.monotonic() - start
    # The backgrounded process lives 3s; the runner must return well before that.
    assert elapsed < 2.0, f"runner hung for {elapsed:.1f}s on a lingering descendant"
    assert result.warning is not None and "background process" in result.warning
    subprocess.run(["pkill", "-f", marker])


# Interrupt recording ---------------------------------------------------------
def test_keyboard_interrupt_is_recorded_and_child_killed(tmp_path, monkeypatch):
    # Make the first wait (in the driver) raise KeyboardInterrupt, as a Ctrl-C
    # would; later waits (reaping in termination) use the real implementation.
    real_wait = subprocess.Popen.wait
    state = {"raised": False}

    def fake_wait(self, timeout=None):
        if not state["raised"]:
            state["raised"] = True
            raise KeyboardInterrupt
        return real_wait(self, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", fake_wait)
    result = run_command(
        f"{PY} -c 'import time; time.sleep(30)'", cwd=tmp_path, echo=False
    )
    assert result.interrupted is True
    assert result.command_data.classification.status == COMMAND_STATUS_INTERRUPTED
    assert result.propagated_exit_code == 130


# Partial PTY allocation cleanup ----------------------------------------------
def test_second_pty_failure_closes_the_first_pair(tmp_path, monkeypatch):
    import os
    import pty

    real_openpty = pty.openpty
    opened: list = []
    state = {"calls": 0}

    def flaky_openpty():
        state["calls"] += 1
        if state["calls"] == 1:
            pair = real_openpty()
            opened.extend(pair)
            return pair
        raise OSError("no more pseudo-terminals")

    monkeypatch.setattr(pty, "openpty", flaky_openpty)
    result = run_command(f"{PY} -c \"print('viapipe')\"", cwd=tmp_path, echo=False)
    # Fell back to pipes and still captured.
    assert "viapipe" in result.command_data.stdout_preview
    # The first pair must have been closed, not leaked.
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


# Process-group termination without a group id --------------------------------
def test_terminate_group_signals_process_when_no_pgid():
    import signal

    from debugbrief.command_runner import _terminate_group

    proc = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
    try:
        _terminate_group(proc, None, (signal.SIGTERM, signal.SIGKILL))
        assert proc.poll() is not None, "process without a pgid was not signalled"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# Interrupt during draining must not escape unrecorded ------------------------
def test_keyboard_interrupt_during_drain_keeps_completed_result(tmp_path, monkeypatch):
    import debugbrief.command_runner as cr

    real_join = cr._join_deadline
    state = {"n": 0}

    def ki_once(readers, seconds):
        state["n"] += 1
        if state["n"] == 1:
            raise KeyboardInterrupt
        return real_join(readers, seconds)

    monkeypatch.setattr(cr, "_join_deadline", ki_once)
    result = run_command(f"{PY} -c \"print('done')\"", cwd=tmp_path, echo=False)
    # The command had already completed: an interrupt during cleanup must not
    # escape, and must not mislabel a finished command as interrupted.
    assert result.interrupted is False
    assert result.command_data.exit_code == 0


def test_interrupt_stores_raw_signal_code(tmp_path, monkeypatch):
    real_wait = subprocess.Popen.wait
    state = {"raised": False}

    def fake_wait(self, timeout=None):
        if not state["raised"]:
            state["raised"] = True
            raise KeyboardInterrupt
        return real_wait(self, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", fake_wait)
    result = run_command(f"{PY} -c 'import time; time.sleep(30)'", cwd=tmp_path, echo=False)
    # Raw reaped signal code is stored (negative), while the CLI exit stays 130.
    assert result.command_data.exit_code is not None
    assert result.command_data.exit_code < 0
    assert result.propagated_exit_code == 130


# Lingering detection via reader state (setsid escapee) -----------------------
def test_setsid_descendant_triggers_warning(tmp_path):
    marker = f"db_setsid_marker_{tmp_path.name}"
    code = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep(3)', '{marker}'], start_new_session=True)"
    )
    start = time.monotonic()
    result = run_command(f'{PY} -c "{code}"', cwd=tmp_path, echo=False)
    elapsed = time.monotonic() - start
    try:
        # The detached child left the process group but still holds the stream;
        # detection is by reader state, so the warning still fires, no hang.
        assert elapsed < 2.0, f"runner hung for {elapsed:.1f}s on a setsid escapee"
        assert result.warning is not None and "background process" in result.warning
    finally:
        subprocess.run(["pkill", "-f", marker])


# Terminal-control sanitizer (the bounded state machine) ----------------------
def _clean(*chunks):
    from debugbrief.command_runner import _TerminalCleaner

    cleaner = _TerminalCleaner()
    return "".join(cleaner.feed(c) for c in chunks) + cleaner.flush()


def test_cleaner_csi_split_across_chunks():
    assert _clean("abc\x1b[3", "1mRED\x1b[0", "m done") == "abcRED done"


def test_cleaner_incomplete_csi_at_eof_is_dropped():
    # An unterminated CSI at end of stream must not leak a raw ESC.
    assert _clean("text\x1b[31") == "text"
    assert "\x1b" not in _clean("x\x1b[")


def test_cleaner_osc8_hyperlink_longer_than_64_chars_split():
    url = "https://example.com/" + "a" * 200
    osc8 = f"\x1b]8;;{url}\x07link text\x1b]8;;\x07"
    # feed it in awkward 7-char chunks
    chunks = [osc8[i : i + 7] for i in range(0, len(osc8), 7)]
    out = _clean(*chunks)
    assert out == "link text"
    assert "\x1b" not in out and "example.com" not in out


def test_cleaner_dcs_split_across_chunks():
    dcs = "\x1bPsome-dcs-payload\x1b\\after"
    chunks = [dcs[i : i + 5] for i in range(0, len(dcs), 5)]
    assert _clean(*chunks) == "after"


def test_cleaner_crlf_and_bare_cr_split_across_chunks():
    assert _clean("line\r", "\nnext") == "line\nnext"  # CRLF split -> one newline
    assert _clean("progress\r", "done") == "progress\ndone"  # bare CR -> newline
    assert _clean("end\r") == "end\n"  # trailing CR at EOF


def test_cleaner_strips_bel_backspace_and_keeps_tab():
    assert _clean("a\x07b\x08c") == "abc"  # BEL and backspace dropped
    assert _clean("col1\tcol2\n") == "col1\tcol2\n"  # tab and newline kept


def test_cleaner_preserves_unicode_adjacent_to_controls():
    assert _clean("\x1b[32mcafé 漢字\x1b[0m 😀") == "café 漢字 😀"


def test_cleaner_oversized_osc_drops_entire_payload():
    # An OSC title far longer than the internal length cap must be dropped in
    # full; the tail past the cap must not leak into the report as text.
    payload = "A" * 5000
    out = _clean(f"before\x1b]0;{payload}\x07after")
    assert out == "beforeafter"
    assert "A" not in out and "\x1b" not in out


def test_cleaner_oversized_osc_drops_payload_when_split():
    # The same oversized OSC fed in tiny chunks (the realistic streaming case)
    # must still be dropped whole, with nothing leaking across read boundaries.
    payload = "Z" * 5000
    osc = f"\x1b]0;{payload}\x07tail"
    chunks = [osc[i : i + 7] for i in range(0, len(osc), 7)]
    out = _clean(*chunks)
    assert out == "tail"
    assert "Z" not in out and "\x1b" not in out


def test_cleaner_oversized_dcs_drops_payload_until_st():
    # DCS/APC/PM/SOS terminate on ST (ESC backslash), not BEL; an oversized one
    # must still be dropped in full, including a terminator past the cap.
    payload = "B" * 5000
    out = _clean(f"\x1bP{payload}\x1b\\done")
    assert out == "done"
    assert "B" not in out and "\x1b" not in out


def test_cleaner_runaway_csi_gives_up_and_resumes_text():
    # A control sequence (CSI) is short by spec, so one that overruns the cap is
    # junk: the cleaner gives up (no raw ESC leaks) and resumes emitting normal
    # text rather than discarding the rest of the stream forever.
    out = _clean("\x1b[" + "1;" * 4000 + "Xdone")
    assert "\x1b" not in out
    assert out.endswith("done")


def test_truncated_colored_output_has_no_escape_fragments(tmp_path):
    prog = (
        "import sys\n"
        "for i in range(500): sys.stdout.write('\\x1b[31m' + str(i) + '\\x1b[0m line\\n')"
    )
    result = run_command(f'{PY} -c "{prog}"', cwd=tmp_path, stdout_limit=120, echo=False)
    preview = result.command_data.stdout_preview
    assert result.command_data.stdout_truncated is True
    assert "\x1b" not in preview  # no escape codes, no split fragments


# Real Ctrl-C at the process level --------------------------------------------
def test_real_sigint_records_interrupted_and_kills_child(tmp_path):
    import json
    import signal

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [PY, "-m", "debugbrief", "start", "sigint test"],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    marker = f"db_real_sigint_{tmp_path.name}"
    proc = subprocess.Popen(
        [PY, "-m", "debugbrief", "run", "--", PY, "-c", f"import time; time.sleep(30)  # {marker}"],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(2.0)
    proc.send_signal(signal.SIGINT)
    rc = proc.wait(timeout=15)
    orphan = _wait_no_orphan(marker)

    assert rc == 130
    assert not orphan, "the interrupted command left an orphaned child"
    sessions = list((tmp_path / ".debugbrief" / "sessions").glob("*.json"))
    assert sessions
    data = json.loads(sessions[0].read_text())
    statuses = [
        e["data"]["classification"]["status"]
        for e in data["events"]
        if e["type"] == "command"
    ]
    assert "interrupted" in statuses


def test_repeated_interrupt_during_termination_still_records(tmp_path, monkeypatch):
    # A second Ctrl-C arriving while the first interrupt is terminating the
    # group must not abandon cleanup and escape unrecorded.
    import debugbrief.command_runner as cr

    real_terminate = cr._terminate_group
    state = {"calls": 0}

    def flaky_terminate(process, pgid, signals):
        state["calls"] += 1
        if state["calls"] == 1:
            raise KeyboardInterrupt  # the second Ctrl-C, during termination
        return real_terminate(process, pgid, signals)

    monkeypatch.setattr(cr, "_terminate_group", flaky_terminate)

    real_wait = subprocess.Popen.wait
    first = {"done": False}

    def fake_wait(self, timeout=None):
        if not first["done"]:
            first["done"] = True
            raise KeyboardInterrupt  # the first Ctrl-C, during wait
        return real_wait(self, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", fake_wait)

    result = run_command(
        f"{PY} -c 'import time; time.sleep(30)'", cwd=tmp_path, echo=False
    )
    assert result.interrupted is True
    assert result.command_data.classification.status == COMMAND_STATUS_INTERRUPTED
    assert state["calls"] >= 2  # the retry actually happened


# Broken downstream pipe (debugbrief run | head) ------------------------------
def test_broken_downstream_pipe_stops_command_promptly(tmp_path):
    import json
    import os

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [PY, "-m", "debugbrief", "start", "bp test"],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    marker = f"db_bp_marker_{tmp_path.name}"
    producer = (
        "import sys, time\n"
        "while True:\n"
        "    sys.stdout.write('line\\n'); sys.stdout.flush()\n"
        "    time.sleep(0.01)\n"
        f"# {marker}\n"
    )
    proc = subprocess.Popen(
        [PY, "-m", "debugbrief", "run", "--timeout", "30", "--", PY, "-c", producer],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline()  # received initial output
    start = time.monotonic()
    proc.stdout.close()  # the consumer (like `head`) goes away
    rc = proc.wait(timeout=15)
    elapsed = time.monotonic() - start

    assert rc == 141, f"expected 141 (SIGPIPE convention), got {rc}"
    assert elapsed < 10, f"did not stop promptly: {elapsed:.1f}s (timeout was 30s)"
    orphan = _wait_no_orphan(marker)
    assert not orphan, "the producer kept running after the pipe broke"

    sessions = list((tmp_path / ".debugbrief" / "sessions").glob("*.json"))
    assert sessions
    data = json.loads(sessions[0].read_text())
    cmds = [e for e in data["events"] if e["type"] == "command"]
    assert len(cmds) == 1, f"expected exactly one command event, got {len(cmds)}"
    assert cmds[0]["data"]["classification"]["status"] == "broken_pipe"
    assert os  # silence unused-import linters across versions


def test_repeated_sigint_persists_exactly_one_event(tmp_path):
    import json
    import signal

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [PY, "-m", "debugbrief", "start", "stress"],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    marker = f"db_stress_marker_{tmp_path.name}"
    proc = subprocess.Popen(
        [PY, "-m", "debugbrief", "run", "--", PY, "-c", f"import time; time.sleep(30)  # {marker}"],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(1.5)
    for _ in range(6):  # hammer Ctrl-C around exit and persistence
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            break
        time.sleep(0.04)
    rc = proc.wait(timeout=15)
    # Best-effort cleanup, not an assertion. A storm SIGINT can kill debugbrief
    # during teardown, before it terminates the child's own session; on a
    # platform without a parent-death signal (macOS) that child is briefly
    # orphaned until it exits on its own, so reap it here rather than require it
    # never happens. The single-interrupt test (test_real_sigint_*) asserts the
    # clean-up guarantee for the realistic one-Ctrl-C case, which is reliable.
    _wait_no_orphan(marker)

    # A storm SIGINT can land at any instant. A clean catch returns 130 and
    # records the interrupted command; a SIGINT that lands during teardown or
    # interpreter shutdown kills the process with signal 2 (wait() reports -2,
    # which a shell also surfaces as 130) and may do so before the event is
    # written or the child is reaped. Both are interrupted outcomes. The
    # guarantee under a storm is integrity, not that cleanup or the write wins
    # the race.
    assert rc in (130, -signal.SIGINT), rc
    sessions = list((tmp_path / ".debugbrief" / "sessions").glob("*.json"))
    assert sessions
    data = json.loads(sessions[0].read_text())  # must remain valid JSON
    cmds = [e for e in data["events"] if e["type"] == "command"]
    # Never more than one command event (no duplication, no half-written event);
    # if the write won the race against the storm, it is recorded as interrupted.
    assert len(cmds) <= 1, f"expected at most one command event, got {len(cmds)}"
    if cmds:
        assert cmds[0]["data"]["classification"]["status"] == "interrupted"
