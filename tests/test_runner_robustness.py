"""Robustness tests for the command runner.

Covers process-tree termination, the no-hang drain for lingering descendants,
bounded retained memory, interrupt recording, and signal exit-code propagation.
"""

from __future__ import annotations

import subprocess
import sys
import time

from debugbrief import filters
from debugbrief.command_runner import RunResult, _BoundedText, run_command
from debugbrief.models import (
    COMMAND_STATUS_INTERRUPTED,
    CommandData,
)

PY = sys.executable


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
    time.sleep(0.5)
    found = subprocess.run(
        ["pgrep", "-f", marker], stdout=subprocess.PIPE
    ).stdout.strip()
    if found:
        subprocess.run(["pkill", "-f", marker])
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
