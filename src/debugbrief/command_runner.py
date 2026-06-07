"""Execute commands via subprocess and capture honest, bounded results.

The runner never fakes an exit code and never claims success it did not observe.
Output is stored as bounded previews (not full logs) and is explicitly flagged
when truncated.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import filters
from .models import CommandData
from .redaction import redact_text
from .utils import (
    DEFAULT_STDERR_PREVIEW_LIMIT,
    DEFAULT_STDOUT_PREVIEW_LIMIT,
    now_iso8601,
    truncate_text,
)

DEFAULT_TIMEOUT_SECONDS = 300


@dataclass
class RunResult:
    """The outcome of running one command, ready to persist and report."""

    command_data: CommandData
    timed_out: bool
    errored: bool
    error_message: Optional[str] = None

    @property
    def propagated_exit_code(self) -> int:
        """Exit code DebugBrief should return to its own caller.

        Real exit codes pass through; timeouts/errors map to a nonzero code so
        callers and scripts see failure.
        """
        code = self.command_data.exit_code
        if code is None:
            return 1
        return code


def _coerce_output(value: Optional[str]) -> str:
    if value is None:
        return ""
    return value


def run_command(
    command: str,
    cwd: Path,
    use_shell: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    stdout_limit: int = DEFAULT_STDOUT_PREVIEW_LIMIT,
    stderr_limit: int = DEFAULT_STDERR_PREVIEW_LIMIT,
    redact: bool = True,
) -> RunResult:
    """Run ``command`` from ``cwd`` and capture a :class:`CommandData`.

    When ``use_shell`` is False (default), the command is parsed with
    ``shlex.split`` and executed without a shell. When ``use_shell`` is True,
    the command runs through the system shell (shell features allowed).

    By default captured output and the command string are passed through
    best-effort secret redaction before they are returned, so raw secrets never
    reach the session file. Pass ``redact=False`` to store the raw text.
    """
    started_at = now_iso8601()
    start_monotonic = time.monotonic()

    timed_out = False
    errored = False
    error_message: Optional[str] = None
    exit_code: Optional[int] = None
    stdout_text = ""
    stderr_text = ""

    popen_args: object
    if use_shell:
        popen_args = command
    else:
        try:
            parsed: List[str] = shlex.split(command)
        except ValueError as exc:
            parsed = []
            errored = True
            error_message = f"Could not parse command: {exc}"
        if not parsed and not errored:
            errored = True
            error_message = "Empty command."
        popen_args = parsed

    if not errored:
        try:
            completed = subprocess.run(
                popen_args,
                cwd=str(cwd),
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
            )
            exit_code = completed.returncode
            stdout_text = _coerce_output(completed.stdout)
            stderr_text = _coerce_output(completed.stderr)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout_text = _coerce_output(
                exc.stdout.decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else exc.stdout
            )
            stderr_text = _coerce_output(
                exc.stderr.decode("utf-8", "replace")
                if isinstance(exc.stderr, bytes)
                else exc.stderr
            )
            error_message = f"Command timed out after {timeout_seconds}s."
        except FileNotFoundError as exc:
            errored = True
            exit_code = None
            error_message = f"Command not found: {exc.filename or command}"
        except PermissionError as exc:
            errored = True
            exit_code = None
            error_message = f"Permission denied: {exc.filename or command}"
        except OSError as exc:  # pragma: no cover - defensive
            errored = True
            exit_code = None
            error_message = f"Failed to execute command: {exc}"

    ended_at = now_iso8601()
    duration = round(time.monotonic() - start_monotonic, 3)

    stdout_preview, stdout_truncated = truncate_text(stdout_text, stdout_limit)
    stderr_preview, stderr_truncated = truncate_text(stderr_text, stderr_limit)

    # Classification is derived from the command tokens and the real exit code,
    # so it is computed before redaction may alter the stored command string.
    classification = filters.classify_command(
        command=command,
        exit_code=exit_code,
        timed_out=timed_out,
        errored=errored,
    )

    stored_command = command
    redacted = False
    if redact:
        stored_command, n_cmd = redact_text(command)
        stdout_preview, n_out = redact_text(stdout_preview)
        stderr_preview, n_err = redact_text(stderr_preview)
        redacted = (n_cmd + n_out + n_err) > 0

    command_data = CommandData(
        command=stored_command,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration,
        exit_code=exit_code,
        stdout_preview=stdout_preview,
        stderr_preview=stderr_preview,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        used_shell=use_shell,
        classification=classification,
        redacted=redacted,
    )

    return RunResult(
        command_data=command_data,
        timed_out=timed_out,
        errored=errored,
        error_message=error_message,
    )
