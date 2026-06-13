"""Execute commands via subprocess and capture honest, bounded results.

The runner never fakes an exit code and never claims success it did not observe.
While the command runs, its stdout and stderr are forwarded to the user's own
terminal as the program writes them, and accumulated in parallel for the stored
previews.

To make that output genuinely live, the command is run under a pseudo-terminal
(one for stdout, one for stderr) rather than a plain pipe. A program decides
whether to line-buffer or block-buffer by asking whether its output is a
terminal; behind a pipe most runtimes block-buffer and the output only appears
when the program exits. A pty makes the program see a terminal, so it streams as
it would in a real shell. Pseudo-terminals are a POSIX feature from the standard
library only (``pty``/``termios``), so this keeps the zero-dependency, Unix-only
design. If a pty cannot be allocated (a locked-down sandbox), the runner falls
back to plain pipes and still captures everything, just without live buffering.

Terminal control sequences (ANSI colors a program emits once it thinks it is on
a terminal) are stripped from the stored previews so reports stay readable; the
live echo keeps them. The previews are bounded (not full logs) and explicitly
flagged when truncated.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, List, Optional, Union

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

# Terminal escape sequences (CSI colors/cursor moves, OSC title sets, and the
# simple two-character escapes) that a program emits when it believes it is
# writing to a terminal. Stripped from stored previews only.
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


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


@dataclass
class _Captured:
    """Internal: the raw outcome of executing a command, before previews."""

    exit_code: Optional[int]
    timed_out: bool
    errored: bool
    error_message: Optional[str]
    stdout_text: str
    stderr_text: str


class _PtyUnavailable(Exception):
    """Raised when a pseudo-terminal cannot be allocated; triggers pipe fallback."""


def _clean_for_storage(text: str) -> str:
    """Normalize captured text for the stored preview.

    Collapses the terminal's CR-LF to LF and strips ANSI escape sequences, so a
    report shows plain readable text rather than color codes. The live echo to
    the user keeps the original bytes.
    """
    if not text:
        return text
    return _ANSI_RE.sub("", text.replace("\r\n", "\n"))


def _popen_error_message(exc: OSError, command: str) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"Command not found: {exc.filename or command}"
    if isinstance(exc, PermissionError):
        return f"Permission denied: {exc.filename or command}"
    return f"Failed to execute command: {exc}"


def _wait_with_readers(
    process: "subprocess.Popen[Any]",
    readers: List[threading.Thread],
    timeout_seconds: int,
) -> tuple:
    """Wait for the process, draining reader threads. Returns (exit_code,
    timed_out, error_message). On timeout the process is killed and whatever was
    accumulated so far is kept."""
    try:
        exit_code = process.wait(timeout=timeout_seconds)
        for reader in readers:
            reader.join()
        return exit_code, False, None
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join(timeout=2.0)
        return None, True, f"Command timed out after {timeout_seconds}s."


def _pump_fd(fd: int, echo_to: Optional[IO[str]], chunks: List[str]) -> None:
    """Drain a pty master ``fd``, echoing live and accumulating.

    Reads bytes (a pty has no text mode), decodes UTF-8 incrementally so a
    multibyte character split across reads is not mangled, echoes the raw text
    to ``echo_to``, and appends it to ``chunks``. On a closed/broken master the
    read raises OSError (macOS reports EIO instead of EOF); both end the loop.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            text = decoder.decode(data)
            if not text:
                continue
            chunks.append(text)
            if echo_to is not None:
                try:
                    echo_to.write(text)
                    echo_to.flush()
                except (OSError, ValueError):
                    echo_to = None
        tail = decoder.decode(b"", final=True)
        if tail:
            chunks.append(tail)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _pump_stream(
    stream: IO[str], echo_to: Optional[IO[str]], chunks: List[str]
) -> None:
    """Drain a text pipe ``stream`` line by line (pipe fallback path)."""
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


def _capture_via_pty(
    popen_args: Union[str, List[str]],
    command: str,
    cwd: Path,
    use_shell: bool,
    timeout_seconds: int,
    echo: bool,
) -> _Captured:
    """Run the command under pseudo-terminals so output streams live.

    Raises :class:`_PtyUnavailable` when a pty cannot be allocated, so the caller
    can fall back to pipes.
    """
    import fcntl
    import pty
    import struct
    import termios

    try:
        out_master, out_slave = pty.openpty()
        err_master, err_slave = pty.openpty()
    except OSError as exc:
        raise _PtyUnavailable(str(exc)) from exc

    for slave in (out_slave, err_slave):
        try:
            attrs = termios.tcgetattr(slave)
            attrs[1] &= ~termios.ONLCR  # do not translate NL to CR-NL
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        except OSError:  # pragma: no cover - termios edge on exotic platforms
            pass

    try:
        process = subprocess.Popen(
            popen_args,
            cwd=str(cwd),
            shell=use_shell,
            stdout=out_slave,
            stderr=err_slave,
            close_fds=True,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        for fd in (out_master, out_slave, err_master, err_slave):
            with contextlib.suppress(OSError):
                os.close(fd)
        return _Captured(None, False, True, _popen_error_message(exc, command), "", "")

    # The child holds the slave ends now; the parent only reads the masters.
    for fd in (out_slave, err_slave):
        with contextlib.suppress(OSError):
            os.close(fd)

    out_chunks: List[str] = []
    err_chunks: List[str] = []
    readers = [
        threading.Thread(
            target=_pump_fd,
            args=(out_master, sys.stdout if echo else None, out_chunks),
            daemon=True,
        ),
        threading.Thread(
            target=_pump_fd,
            args=(err_master, sys.stderr if echo else None, err_chunks),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    exit_code, timed_out, error_message = _wait_with_readers(
        process, readers, timeout_seconds
    )
    return _Captured(
        exit_code, timed_out, False, error_message, "".join(out_chunks), "".join(err_chunks)
    )


def _capture_via_pipes(
    popen_args: Union[str, List[str]],
    command: str,
    cwd: Path,
    use_shell: bool,
    timeout_seconds: int,
    echo: bool,
) -> _Captured:
    """Run the command with plain pipes (fallback when no pty is available).

    Output is still captured and echoed, but a program that block-buffers when
    it is not on a terminal will only appear once it exits.
    """
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
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return _Captured(None, False, True, _popen_error_message(exc, command), "", "")

    out_chunks: List[str] = []
    err_chunks: List[str] = []
    readers = [
        threading.Thread(
            target=_pump_stream,
            args=(process.stdout, sys.stdout if echo else None, out_chunks),
            daemon=True,
        ),
        threading.Thread(
            target=_pump_stream,
            args=(process.stderr, sys.stderr if echo else None, err_chunks),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    exit_code, timed_out, error_message = _wait_with_readers(
        process, readers, timeout_seconds
    )
    return _Captured(
        exit_code, timed_out, False, error_message, "".join(out_chunks), "".join(err_chunks)
    )


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

    The command runs under a pseudo-terminal so its stdout and stderr stream
    live to the corresponding ``sys`` streams (disable with ``echo=False``),
    falling back to plain pipes where no pty is available. The echo is the raw
    output; only the stored previews are cleaned and redacted.

    By default captured output and the command string are passed through
    best-effort secret redaction before they are returned, so raw secrets never
    reach the session file. Pass ``redact=False`` to store the raw text.

    ``force_verification`` marks an unrecognized command as a declared check
    (tool ``custom``); pass/fail honesty is unaffected.
    """
    started_at = now_iso8601()
    start_monotonic = time.monotonic()

    errored = False
    error_message: Optional[str] = None

    popen_args: Union[str, List[str]]
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

    if errored:
        captured = _Captured(None, False, True, error_message, "", "")
    else:
        try:
            captured = _capture_via_pty(
                popen_args, command, cwd, use_shell, timeout_seconds, echo
            )
        except _PtyUnavailable:
            captured = _capture_via_pipes(
                popen_args, command, cwd, use_shell, timeout_seconds, echo
            )

    ended_at = now_iso8601()
    duration = round(time.monotonic() - start_monotonic, 3)

    stdout_clean = _clean_for_storage(captured.stdout_text)
    stderr_clean = _clean_for_storage(captured.stderr_text)
    stdout_preview, stdout_truncated = truncate_text(stdout_clean, stdout_limit)
    stderr_preview, stderr_truncated = truncate_text(stderr_clean, stderr_limit)

    # Classification is derived from the command tokens and the real exit code,
    # so it is computed before redaction may alter the stored command string.
    classification = filters.classify_command(
        command=command,
        exit_code=captured.exit_code,
        timed_out=captured.timed_out,
        errored=captured.errored,
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
        exit_code=captured.exit_code,
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
        timed_out=captured.timed_out,
        errored=captured.errored,
        error_message=captured.error_message,
    )
