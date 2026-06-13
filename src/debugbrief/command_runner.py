"""Execute commands via subprocess and capture honest, bounded results.

The runner never fakes an exit code and never claims success it did not observe.
While the command runs, its stdout and stderr are forwarded to the user's own
terminal, line by line and unmodified, as fast as the program writes them. Note
that a program which block-buffers its output when it is not attached to a
terminal (a plain Python script, for example) will appear in one burst at the
end; that is the program's own buffering, not the runner withholding output. The
full output is accumulated in parallel and stored as bounded previews (not full
logs), explicitly flagged when truncated.
"""

from __future__ import annotations

import contextlib
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, List, Optional

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


def _pump_stream(
    stream: IO[str], echo_to: Optional[IO[str]], chunks: List[str]
) -> None:
    """Drain ``stream`` line by line, echoing live and accumulating.

    Runs on a daemon reader thread, one per pipe. Lines are passed through to
    ``echo_to`` unmodified (it is the user's own terminal) and appended to
    ``chunks`` for the stored preview. A broken or closed echo target stops the
    echo but never the capture.
    """
    try:
        for line in iter(stream.readline, ""):
            chunks.append(line)
            if echo_to is not None:
                try:
                    echo_to.write(line)
                    echo_to.flush()
                except (OSError, ValueError):
                    echo_to = None
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def run_command(
    command: str,
    cwd: Path,
    use_shell: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    stdout_limit: int = DEFAULT_STDOUT_PREVIEW_LIMIT,
    stderr_limit: int = DEFAULT_STDERR_PREVIEW_LIMIT,
    redact: bool = True,
    echo: bool = True,
    force_verification: bool = False,
) -> RunResult:
    """Run ``command`` from ``cwd`` and capture a :class:`CommandData`.

    When ``use_shell`` is False (default), the command is parsed with
    ``shlex.split`` and executed without a shell. When ``use_shell`` is True,
    the command runs through the system shell (shell features allowed).

    While the command runs its stdout and stderr are echoed live to the
    corresponding ``sys`` streams (disable with ``echo=False``). The echo is the
    raw output; only the stored previews are redacted.

    By default captured output and the command string are passed through
    best-effort secret redaction before they are returned, so raw secrets never
    reach the session file. Pass ``redact=False`` to store the raw text.

    ``force_verification`` marks an unrecognized command as a declared check
    (tool ``custom``); pass/fail honesty is unaffected.
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

    process: Optional["subprocess.Popen[str]"] = None
    if not errored:
        try:
            process = subprocess.Popen(
                popen_args,
                cwd=str(cwd),
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,
            )
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

    if process is not None:
        stdout_chunks: List[str] = []
        stderr_chunks: List[str] = []
        readers = [
            threading.Thread(
                target=_pump_stream,
                args=(process.stdout, sys.stdout if echo else None, stdout_chunks),
                daemon=True,
            ),
            threading.Thread(
                target=_pump_stream,
                args=(process.stderr, sys.stderr if echo else None, stderr_chunks),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            exit_code = process.wait(timeout=timeout_seconds)
            # Normal exit: drain whatever is left in the pipes before moving on.
            for reader in readers:
                reader.join()
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            process.kill()
            process.wait()
            # Join briefly and keep whatever partial output was accumulated.
            for reader in readers:
                reader.join(timeout=2.0)
            error_message = f"Command timed out after {timeout_seconds}s."
        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)

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
        force_verification=force_verification,
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
