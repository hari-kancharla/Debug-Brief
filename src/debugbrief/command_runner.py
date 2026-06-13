"""Execute commands via subprocess and capture honest, bounded results.

The runner never fakes an exit code and never claims success it did not observe.
While the command runs, its stdout and stderr are forwarded to the user's own
terminal as the program writes them, and accumulated in parallel for the stored
previews.

Design notes for the parts that are easy to get wrong:

- Live output: the command runs under a pseudo-terminal (one for stdout, one for
  stderr). A program decides whether to line-buffer or block-buffer by asking
  whether its output is a terminal; behind a plain pipe most runtimes block-buffer
  and nothing appears until they exit. A pty makes them stream as in a real shell.
  Where no pty can be allocated (a locked-down sandbox), capture falls back to
  plain pipes and still works, just without live buffering.

- Process tree: the command runs in its own session/process group
  (``start_new_session``), so a timeout or a Ctrl-C terminates the whole group
  with ``killpg``, not just the immediate process. A command that spawns
  background children no longer leaves them running.

- No hang: after the immediate process exits, a background descendant can keep
  the output streams open. The runner detects a still-living process group,
  drains what is buffered, and returns with a warning instead of blocking
  forever on the open stream.

- Bounded memory: output is accumulated through a head-and-tail buffer that
  retains at most the preview budget, so a command that prints gigabytes does
  not grow the runner's memory. The preview limits bound the process, not only
  the stored file.

Terminal control sequences are stripped from the stored previews so reports stay
readable; the live echo keeps them. Pseudo-terminals, process groups, and
signals are POSIX standard library only, so this keeps the zero-dependency,
Unix-only design.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, List, Optional, Tuple, Union

from . import filters
from .models import CommandData
from .redaction import redact_text
from .utils import (
    DEFAULT_STDERR_PREVIEW_LIMIT,
    DEFAULT_STDOUT_PREVIEW_LIMIT,
    now_iso8601,
)

DEFAULT_TIMEOUT_SECONDS = 300

# How long to let readers drain buffered output once the command's immediate
# process has exited but a background descendant still holds the stream open.
_LINGER_DRAIN_SECONDS = 0.5
# Upper bound on joining a reader once the writers are gone (EOF is near-instant;
# this is only a safety cap).
_READER_JOIN_SECONDS = 2.0
# How long to wait for a signalled process group to die before escalating.
_GROUP_TERM_WAIT = 2.0
# Granularity at which readers poll for output and for the stop signal.
_SELECT_INTERVAL = 0.2

# Terminal escape sequences (CSI colors/cursor moves, OSC title sets, and the
# simple two-character escapes) a program emits when it believes it is on a
# terminal. Stripped from stored previews only; the live echo keeps them.
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


@dataclass
class RunResult:
    """The outcome of running one command, ready to persist and report."""

    command_data: CommandData
    timed_out: bool
    errored: bool
    error_message: Optional[str] = None
    interrupted: bool = False
    warning: Optional[str] = None

    @property
    def propagated_exit_code(self) -> int:
        """Exit code DebugBrief should return to its own caller.

        Real exit codes pass through. A command killed by signal ``N`` is
        reported by the OS as ``-N``; the shell convention is ``128 + N`` (so
        SIGINT becomes 130), and that is what callers and scripts expect.
        Interrupts map to 130, and timeouts/errors (no exit code) to 1.
        """
        if self.interrupted:
            return 130
        code = self.command_data.exit_code
        if code is None:
            return 1
        if code < 0:
            return 128 + (-code)
        return code


class _PtyUnavailable(Exception):
    """Raised when a pseudo-terminal cannot be allocated; triggers pipe fallback."""


class _BoundedText:
    """Accumulate streamed text while retaining at most a bounded amount.

    Keeps the first ``limit`` characters (enough to reproduce the whole text
    when it is short) and, separately, the last ``limit - limit // 3``
    characters, so a head-and-tail preview can be produced without ever holding
    the full output in memory. A ``limit`` of zero or less means "no limit" and
    everything is kept (the caller opted out of bounding).
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.unbounded = limit is None or limit <= 0
        if self.unbounded:
            self._parts: List[str] = []
        else:
            self.head_len = limit // 3
            self.tail_len = limit - self.head_len
            self._prefix = ""  # first `limit` characters
            self._tail = ""  # last `tail_len` characters

    def feed(self, text: str) -> None:
        if not text:
            return
        self.total += len(text)
        if self.unbounded:
            self._parts.append(text)
            return
        if len(self._prefix) < self.limit:
            self._prefix += text[: self.limit - len(self._prefix)]
        if self.tail_len:
            self._tail = (self._tail + text)[-self.tail_len :]

    def result(self) -> Tuple[str, bool]:
        """Return (preview, was_truncated)."""
        if self.unbounded:
            return "".join(self._parts), False
        if self.total <= self.limit:
            return self._prefix, False
        head = self._prefix[: self.head_len]
        omitted = self.total - self.limit
        marker = f"\n... [{omitted} characters omitted] ...\n"
        return head + marker + self._tail, True


def _clean_for_storage(text: str) -> str:
    """Normalize captured text for the stored preview.

    Collapses the terminal's CR-LF to LF and strips ANSI escape sequences, so a
    report shows plain readable text rather than color codes.
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


def _group_alive(pgid: Optional[int]) -> bool:
    """True if any process remains in the group ``pgid``."""
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but not ours
        return True


def _terminate_group(
    process: "subprocess.Popen[Any]", pgid: Optional[int], signals: Tuple[int, ...]
) -> None:
    """Signal the whole process group, escalating until it is gone.

    SIGKILL (the last signal callers pass) cannot be caught, so the group is
    guaranteed dead by the end. Falls back to signalling just the immediate
    process if no group id is known.
    """
    for sig in signals:
        if not _group_alive(pgid):
            break
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                process.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            break
        try:
            process.wait(timeout=_GROUP_TERM_WAIT)
        except subprocess.TimeoutExpired:
            continue
        else:
            if not _group_alive(pgid):
                return
    with contextlib.suppress(Exception):
        process.poll()


def _join_deadline(readers: List[threading.Thread], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))


def _pump_fd(
    fd: int, echo_to: Optional[IO[str]], bounded: _BoundedText, stop: threading.Event
) -> None:
    """Drain an output ``fd`` until EOF or ``stop``, echoing live and accumulating.

    Uses ``select`` so the loop can notice ``stop`` even when no data arrives
    (a background process holding the stream open). Decodes UTF-8 incrementally
    so a character split across reads is not mangled. Reads bytes (a pty master
    has no text mode) and never closes the fd; the caller owns it.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while not stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], _SELECT_INTERVAL)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:  # closed master / EIO on macOS == EOF
                break
            if not data:
                break
            text = decoder.decode(data)
            if not text:
                continue
            bounded.feed(text)
            if echo_to is not None:
                try:
                    echo_to.write(text)
                    echo_to.flush()
                except (OSError, ValueError):
                    echo_to = None
        tail = decoder.decode(b"", final=True)
        if tail:
            bounded.feed(tail)
    except Exception:  # pragma: no cover - a reader thread must never crash the run
        pass


def _drive(
    process: "subprocess.Popen[Any]",
    pgid: Optional[int],
    specs: List[Tuple[int, Optional[IO[str]], _BoundedText]],
    timeout_seconds: int,
) -> Tuple[Optional[int], bool, bool, Optional[str], Optional[str]]:
    """Run reader threads, wait for the process, and wind everything down.

    Returns (exit_code, timed_out, interrupted, error_message, warning).
    """
    stop = threading.Event()
    readers = [
        threading.Thread(target=_pump_fd, args=(fd, echo, bounded, stop), daemon=True)
        for (fd, echo, bounded) in specs
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    interrupted = False
    error_message: Optional[str] = None
    warning: Optional[str] = None
    exit_code: Optional[int] = None

    try:
        exit_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(process, pgid, (signal.SIGTERM, signal.SIGKILL))
        error_message = f"Command timed out after {timeout_seconds}s."
    except KeyboardInterrupt:
        interrupted = True
        _terminate_group(process, pgid, (signal.SIGINT, signal.SIGTERM, signal.SIGKILL))
        error_message = "Command was interrupted before it finished."

    if not timed_out and not interrupted and _group_alive(pgid):
        # The immediate process is done but a background descendant still holds
        # the streams open. Drain what is buffered, then stop instead of hanging.
        _join_deadline(readers, _LINGER_DRAIN_SECONDS)
        if any(reader.is_alive() for reader in readers):
            warning = (
                "Output stream stayed open after the command exited; a "
                "background process it started may still be running, so the "
                "captured output may be incomplete."
            )
    else:
        _join_deadline(readers, _READER_JOIN_SECONDS)

    stop.set()
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SECONDS)
    return exit_code, timed_out, interrupted, error_message, warning


def _capture_via_pty(
    popen_args: Union[str, List[str]],
    command: str,
    cwd: Path,
    use_shell: bool,
    timeout_seconds: int,
    echo: bool,
    out_bounded: _BoundedText,
    err_bounded: _BoundedText,
) -> Tuple[Optional[int], bool, bool, Optional[str], Optional[str]]:
    """Run under pseudo-terminals so output streams live. Raises
    :class:`_PtyUnavailable` when a pty cannot be allocated."""
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
            start_new_session=True,
            close_fds=True,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        for fd in (out_master, out_slave, err_master, err_slave):
            with contextlib.suppress(OSError):
                os.close(fd)
        return None, False, False, _popen_error_message(exc, command), None

    # The child holds the slave ends now; the parent only reads the masters.
    for fd in (out_slave, err_slave):
        with contextlib.suppress(OSError):
            os.close(fd)

    try:
        specs = [
            (out_master, sys.stdout if echo else None, out_bounded),
            (err_master, sys.stderr if echo else None, err_bounded),
        ]
        return _drive(process, process.pid, specs, timeout_seconds)
    finally:
        for fd in (out_master, err_master):
            with contextlib.suppress(OSError):
                os.close(fd)


def _capture_via_pipes(
    popen_args: Union[str, List[str]],
    command: str,
    cwd: Path,
    use_shell: bool,
    timeout_seconds: int,
    echo: bool,
    out_bounded: _BoundedText,
    err_bounded: _BoundedText,
) -> Tuple[Optional[int], bool, bool, Optional[str], Optional[str]]:
    """Run with plain pipes (fallback when no pty is available). Output is still
    captured and echoed, but a program that block-buffers off a terminal only
    appears once it exits."""
    try:
        process = subprocess.Popen(
            popen_args,
            cwd=str(cwd),
            shell=use_shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
            close_fds=True,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return None, False, False, _popen_error_message(exc, command), None

    assert process.stdout is not None and process.stderr is not None
    try:
        specs = [
            (process.stdout.fileno(), sys.stdout if echo else None, out_bounded),
            (process.stderr.fileno(), sys.stderr if echo else None, err_bounded),
        ]
        return _drive(process, process.pid, specs, timeout_seconds)
    finally:
        for stream in (process.stdout, process.stderr):
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
    the command runs through the system shell.

    The command runs under a pseudo-terminal in its own process group, so its
    output streams live (disable echo with ``echo=False``) and a timeout or a
    Ctrl-C terminates the whole tree. Output is accumulated through a bounded
    buffer, so the runner's memory stays bounded regardless of how much the
    command prints. Pass ``redact=False`` to store output verbatim;
    ``force_verification`` marks an unrecognized command as a declared check.
    """
    started_at = now_iso8601()
    start_monotonic = time.monotonic()

    errored = False
    error_message: Optional[str] = None
    interrupted = False
    timed_out = False
    warning: Optional[str] = None
    exit_code: Optional[int] = None

    out_bounded = _BoundedText(stdout_limit)
    err_bounded = _BoundedText(stderr_limit)

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

    if not errored:
        args = (
            popen_args, command, cwd, use_shell, timeout_seconds, echo,
            out_bounded, err_bounded,
        )
        try:
            exit_code, timed_out, interrupted, error_message, warning = _capture_via_pty(*args)
        except _PtyUnavailable:
            exit_code, timed_out, interrupted, error_message, warning = _capture_via_pipes(*args)
        errored = error_message is not None and not timed_out and not interrupted

    ended_at = now_iso8601()
    duration = round(time.monotonic() - start_monotonic, 3)

    stdout_raw, stdout_truncated = out_bounded.result()
    stderr_raw, stderr_truncated = err_bounded.result()
    stdout_preview = _clean_for_storage(stdout_raw)
    stderr_preview = _clean_for_storage(stderr_raw)

    # Classification is derived from the command tokens and the real outcome, so
    # it is computed before redaction may alter the stored command string.
    classification = filters.classify_command(
        command=command,
        exit_code=exit_code,
        timed_out=timed_out,
        errored=errored,
        force_verification=force_verification,
        interrupted=interrupted,
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
        interrupted=interrupted,
        warning=warning,
    )
