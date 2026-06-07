"""Tests for the subprocess command runner."""

from __future__ import annotations

import sys

from debugbrief import command_runner
from debugbrief.command_runner import run_command
from debugbrief.models import (
    COMMAND_STATUS_ERROR,
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_PASSED,
    COMMAND_STATUS_TIMED_OUT,
)

PY = sys.executable


def test_run_success(tmp_path):
    result = run_command(f"{PY} -c \"print('ok')\"", cwd=tmp_path)
    assert result.errored is False
    assert result.timed_out is False
    data = result.command_data
    assert data.exit_code == 0
    assert "ok" in data.stdout_preview
    assert data.classification.status == COMMAND_STATUS_PASSED
    assert result.propagated_exit_code == 0


def test_run_failure_preserves_exit_code(tmp_path):
    result = run_command(f"{PY} -c \"import sys; sys.exit(7)\"", cwd=tmp_path)
    assert result.command_data.exit_code == 7
    assert result.command_data.classification.status == COMMAND_STATUS_FAILED
    assert result.propagated_exit_code == 7


def test_run_captures_stderr(tmp_path):
    result = run_command(
        f"{PY} -c \"import sys; sys.stderr.write('boom')\"", cwd=tmp_path
    )
    assert "boom" in result.command_data.stderr_preview


def test_run_timeout(tmp_path):
    result = run_command(
        f"{PY} -c \"import time; time.sleep(5)\"",
        cwd=tmp_path,
        timeout_seconds=1,
    )
    assert result.timed_out is True
    data = result.command_data
    assert data.exit_code is None
    assert data.classification.status == COMMAND_STATUS_TIMED_OUT
    assert result.propagated_exit_code != 0


def test_run_command_not_found(tmp_path):
    result = run_command("definitely_not_a_real_binary_xyz123", cwd=tmp_path)
    assert result.errored is True
    assert result.command_data.exit_code is None
    assert result.command_data.classification.status == COMMAND_STATUS_ERROR
    assert result.propagated_exit_code != 0
    assert result.error_message and "not found" in result.error_message.lower()


def test_run_empty_command_is_error(tmp_path):
    result = run_command("   ", cwd=tmp_path)
    assert result.errored is True
    assert result.command_data.exit_code is None


def test_stdout_truncation(tmp_path):
    result = run_command(
        f"{PY} -c \"print('x' * 100)\"",
        cwd=tmp_path,
        stdout_limit=10,
    )
    data = result.command_data
    assert data.stdout_truncated is True
    assert len(data.stdout_preview) == 10


def test_shell_mode_supports_pipes(tmp_path):
    result = run_command("echo hello | tr a-z A-Z", cwd=tmp_path, use_shell=True)
    assert result.command_data.exit_code == 0
    assert "HELLO" in result.command_data.stdout_preview
    assert result.command_data.used_shell is True


def test_command_string_preserved_verbatim(tmp_path):
    cmd = f"{PY} -c \"print('verbatim test 123')\""
    result = run_command(cmd, cwd=tmp_path)
    assert result.command_data.command == cmd


def test_default_timeout_constant():
    assert command_runner.DEFAULT_TIMEOUT_SECONDS == 300
