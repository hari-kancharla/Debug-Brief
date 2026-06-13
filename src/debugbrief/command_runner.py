"""Execute commands via subprocess and capture honest, bounded results.

The runner never fakes an exit code and never claims success it did not observe.
While the command runs, its stdout and stderr are forwarded to the user's own
terminal as the program writes them, and a bounded preview of each is retained
for the report.

Design notes for the parts that are easy to get wrong:

- Live output: the command runs under a pseudo-terminal (one for stdout, one for
  stderr). A program decides whether to line-buffer or block-buffer by asking
  whether its output is a terminal; behind a plain pipe most runtimes
  block-buffer and nothing appears until they exit. A pty gives terminal-like
  buffering, so output streams as it is written. Where no pty can be allocated
  (a locked-down sandbox), capture falls back to plain pipes.

- Process group: the command runs in its own session/process group
  (``start_new_session``), so a timeout, a Ctrl-C, or a broken downstream pipe
  terminates the group with ``killpg``, not just the immediate process. Ordinary
  background children are cleaned up. A child that detaches into its own session
  and keeps a captured stream open is reported as a warning; one that also closes
  its inherited output descriptors can outlive the command undetectably.

- Broken downstream pipe: when DebugBrief's own stdout (the consumer side of
  ``debugbrief run ... | head``) closes, the command would otherwise keep writing
  into the pty and run to its timeout. The runner notices the failed echo,
  terminates the command, and returns the broken-pipe status with code 141.

- No hang: after the immediate process exits, a background process can keep the
  output streams open. The runner drains what is buffered, then stops instead of
  blocking forever, and warns that the captured output may be incomplete.

- Bounded memory: output is retained through a head-and-tail buffer capped at the
  preview budget, so a command that prints gigabytes does not grow the runner's
  memory.

Terminal control sequences and unsafe control characters are stripped from the
stored preview by a small bounded state machine as output is read (the live echo
keeps them), so a sequence split across reads or the truncation boundary never
leaves a fragment in the report. Pseudo-terminals, process groups, and signals
are POSIX standard library only, so this keeps the zero-dependency, Unix-only
design.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import select
import shlex
import signal
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
)

DEFAULT_TIMEOUT_SECONDS = 300

# How long to let readers drain buffered output once the command's immediate
# process has exited but a background process still holds the stream open.
_LINGER_DRAIN_SECONDS = 0.5
# Upper bound on joining a reader once the writers are gone (EOF is near-instant;
# this is only a safety cap).
_READER_JOIN_SECONDS = 2.0
# How long to wait for a signalled process group to die before escalating.
_GROUP_TERM_WAIT = 2.0
# Granularity at which readers poll for output and the driver polls for exit.
_SELECT_INTERVAL = 0.2
_WAIT_POLL_INTERVAL = 0.1


@dataclass
class RunResult:
    """The outcome of running one command, ready to persist and report."""

    command_data: CommandData
    timed_out: bool
    errored: bool
    error_message: Optional[str] = None
    interrupted: bool = False
    broken_pipe: bool = False
    warning: Optional[str] = None

    @property
    def propagated_exit_code(self) -> int:
        """Exit code DebugBrief should return to its own caller.

        A user interrupt maps to 130 and a broken downstream pipe to 141, the
        shell conventions. Otherwise real exit codes pass through, a command
        killed by signal ``N`` becomes ``128 + N``, and a timeout or spawn error
        (no exit code) becomes 1.
        """
        if self.interrupted:
            return 130
        if self.broken_pipe:
            return 141
        code = self.command_data.exit_code
        if code is None:
            return 1
        if code < 0:
            return 128 + (-code)
        return code


@dataclass
class _Outcome:
    """Internal: the raw result of driving a command to completion."""

    exit_code: Optional[int] = None
    timed_out: bool = False
    interrupted: bool = False
    broken_pipe: bool = False
    error_message: Optional[str] = None
    warning: Optional[str] = None


class _PtyUnavailable(Exception):
    """Raised when a pseudo-terminal cannot be allocated; triggers pipe fallback."""


class _TerminalCleaner:
    """Streaming sanitizer that strips terminal control from stored output.

    A small state machine removes ANSI/terminal escape sequences (CSI, OSC,
    DCS/APC/PM/SOS) and unsafe C0/C1 control characters, so the report is plain
    linear text. It handles sequences split across arbitrary read chunks with a
    fixed memory bound and never emits a raw ESC from an incomplete or oversized
    sequence. CR-LF and a bare CR are normalized to LF even across chunk
    boundaries; newline and tab are preserved; normal Unicode passes through.
    The live echo is fed the raw bytes separately, so color is unaffected.
    """

    _GROUND, _ESC, _ESC_INT, _CSI, _OSC, _STR = range(6)
    _MAX_SEQ = 4096  # give up on a runaway control sequence; strings run to their terminator

    def __init__(self) -> None:
        self._state = self._GROUND
        self._pending_cr = False
        self._seq = 0
        self._esc_seen = False  # saw ESC inside OSC/STR (ST is ESC then backslash)

    def feed(self, text: str) -> str:
        out: List[str] = []
        for ch in text:
            self._step(ch, out)
        return "".join(out)

    def flush(self) -> str:
        out: List[str] = []
        if self._pending_cr:
            out.append("\n")
        # Drop any sequence still in progress rather than emit a raw ESC.
        self._reset()
        self._pending_cr = False
        return "".join(out)

    def _reset(self) -> None:
        self._state = self._GROUND
        self._seq = 0
        self._esc_seen = False

    def _ground_char(self, ch: str, out: List[str]) -> None:
        if ch in ("\n", "\t"):
            out.append(ch)
            return
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            return  # other C0 controls, DEL, and C1 controls: drop
        out.append(ch)

    def _step(self, ch: str, out: List[str]) -> None:
        if self._pending_cr:
            self._pending_cr = False
            out.append("\n")
            if ch == "\n":
                return  # CR-LF collapsed into the single newline already emitted
            # bare CR: newline emitted, now process ch normally below

        if self._state == self._GROUND:
            if ch == "\x1b":
                self._state = self._ESC
                self._seq = 1
            elif ch == "\r":
                self._pending_cr = True
            else:
                self._ground_char(ch, out)
            return

        # Inside an escape sequence: enforce the length bound. A control
        # sequence (CSI/ESC/ESC_INT) is short by spec, so an overrun is junk:
        # give up and resume normal output. A string sequence
        # (OSC/DCS/APC/PM/SOS) is bounded by its explicit terminator (BEL or
        # ST), not by length, and may be legitimately long; keep waiting for
        # the terminator rather than returning to ground, which would leak the
        # remaining payload into the report as text.
        self._seq += 1
        if self._seq > self._MAX_SEQ and self._state not in (self._OSC, self._STR):
            self._reset()
            return

        code = ord(ch)
        if self._state == self._ESC:
            if ch == "[":
                self._state = self._CSI
            elif ch == "]":
                self._state = self._OSC
                self._esc_seen = False
            elif ch in ("P", "X", "^", "_"):
                self._state = self._STR
                self._esc_seen = False
            elif 0x20 <= code <= 0x2F:
                self._state = self._ESC_INT
            else:
                self._reset()  # two-character escape or junk: done, dropped
        elif self._state == self._ESC_INT:
            if 0x30 <= code <= 0x7E:
                self._reset()  # final byte
            elif not (0x20 <= code <= 0x2F):
                self._reset()  # malformed
        elif self._state == self._CSI:
            if 0x40 <= code <= 0x7E:
                self._reset()  # final byte
            elif not (0x20 <= code <= 0x3F):
                self._reset()  # not a parameter/intermediate byte: malformed
        elif self._state == self._OSC:
            if code == 0x07:  # BEL terminates an OSC
                self._reset()
            elif ch == "\x1b":
                self._esc_seen = True
            elif self._esc_seen:
                self._esc_seen = False
                if ch == "\\":  # ST (ESC backslash) terminates
                    self._reset()
        elif self._state == self._STR:
            if ch == "\x1b":
                self._esc_seen = True
            elif self._esc_seen:
                self._esc_seen = False
                if ch == "\\":  # ST terminates DCS/APC/PM/SOS
                    self._reset()


class _BoundedText:
    """Accumulate streamed text while retaining at most a bounded amount.

    Keeps the first ``limit`` characters (enough to reproduce the whole text
    when it is short) and, separately, the last ``limit - limit // 3``
    characters, so a head-and-tail preview can be produced without ever holding
    the full output in memory. A ``limit`` of zero or less means "no limit".
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
            self._prefix = ""
            self._tail = ""

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

    def result(self) -> "tuple[str, bool]":
        if self.unbounded:
            return "".join(self._parts), False
        if self.total <= self.limit:
            return self._prefix, False
        head = self._prefix[: self.head_len]
        omitted = self.total - self.limit
        marker = f"\n... [{omitted} characters omitted] ...\n"
        return head + marker + self._tail, True


def _popen_error_message(exc: OSError, command: str) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"Command not found: {exc.filename or command}"
    if isinstance(exc, PermissionError):
        return f"Permission denied: {exc.filename or command}"
    return f"Failed to execute command: {exc}"


def _group_alive(pgid: Optional[int]) -> bool:
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but not ours
        return True


def _still_running(process: "subprocess.Popen[Any]", pgid: Optional[int]) -> bool:
    if pgid is not None:
        return _group_alive(pgid)
    return process.poll() is None


def _terminate_group(
    process: "subprocess.Popen[Any]", pgid: Optional[int], signals: "tuple[int, ...]"
) -> None:
    """Signal the process group, escalating until it is gone.

    The last signal callers pass is SIGKILL, which cannot be caught, so the group
    is gone by the end. Falls back to signalling the immediate process when no
    group id is known.
    """
    for sig in signals:
        if not _still_running(process, pgid):
            break
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                process.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            break
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_GROUP_TERM_WAIT)
        if not _still_running(process, pgid):
            break
    with contextlib.suppress(Exception):
        process.poll()


def _join_deadline(readers: List[threading.Thread], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))


def _ignore_interrupt(func: Any, *args: Any) -> Any:
    """Call ``func(*args)``, retrying if a KeyboardInterrupt arrives.

    Teardown must complete even if the user keeps pressing Ctrl-C, so a second
    interrupt does not abandon cleanup and leave the command unrecorded. Each
    retried operation is bounded, so this cannot spin forever.
    """
    while True:
        try:
            return func(*args)
        except KeyboardInterrupt:
            continue


def _pump_fd(
    fd: int,
    echo_to: Optional[IO[str]],
    bounded: _BoundedText,
    stop: threading.Event,
    broken_pipe: Optional[threading.Event],
) -> None:
    """Drain an output ``fd`` until EOF or ``stop``, echoing live and accumulating.

    Echoes the raw bytes (the user's terminal keeps any color) and feeds a
    cleaned copy to ``bounded``. If the live echo fails because the downstream
    consumer closed the pipe, ``broken_pipe`` is signalled so the driver can stop
    the command instead of letting it run to its timeout. Uses ``select`` so the
    loop notices ``stop`` even when no data arrives, decodes UTF-8 incrementally,
    and never closes the fd; the caller owns it.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    cleaner = _TerminalCleaner()

    def emit(raw: str) -> None:
        nonlocal echo_to
        if echo_to is not None:
            try:
                echo_to.write(raw)
                echo_to.flush()
            except BrokenPipeError:
                echo_to = None
                if broken_pipe is not None:
                    broken_pipe.set()
            except (OSError, ValueError):
                echo_to = None
        bounded.feed(cleaner.feed(raw))

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
            if text:
                emit(text)
        tail = decoder.decode(b"", final=True)
        if tail:
            emit(tail)
        bounded.feed(cleaner.flush())
    except Exception:  # pragma: no cover - a reader thread must never crash the run
        pass


def _drive(
    process: "subprocess.Popen[Any]",
    pgid: Optional[int],
    out_fd: int,
    err_fd: int,
    out_bounded: _BoundedText,
    err_bounded: _BoundedText,
    echo: bool,
    timeout_seconds: int,
) -> _Outcome:
    """Run reader threads, wait for the process, and wind everything down."""
    stop = threading.Event()
    broken_pipe = threading.Event()
    readers = [
        threading.Thread(
            target=_pump_fd,
            args=(out_fd, sys.stdout if echo else None, out_bounded, stop, broken_pipe),
            daemon=True,
        ),
        threading.Thread(
            target=_pump_fd,
            args=(err_fd, sys.stderr if echo else None, err_bounded, stop, None),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    out = _Outcome()
    completed = False
    deadline = time.monotonic() + timeout_seconds

    try:
        # Poll so we can also notice a broken downstream pipe or the timeout.
        while True:
            try:
                out.exit_code = process.wait(timeout=_WAIT_POLL_INTERVAL)
                completed = True
                break
            except subprocess.TimeoutExpired:
                if broken_pipe.is_set():
                    out.broken_pipe = True
                    _terminate_group(
                        process,
                        pgid,
                        (signal.SIGPIPE, signal.SIGTERM, signal.SIGKILL),
                    )
                    out.error_message = (
                        "The downstream consumer closed the pipe; the command was "
                        "stopped."
                    )
                    break
                if time.monotonic() >= deadline:
                    out.timed_out = True
                    _terminate_group(
                        process, pgid, (signal.SIGTERM, signal.SIGKILL)
                    )
                    out.error_message = (
                        f"Command timed out after {timeout_seconds}s."
                    )
                    break

        drain = _LINGER_DRAIN_SECONDS if completed else _READER_JOIN_SECONDS
        _join_deadline(readers, drain)
        if completed and any(reader.is_alive() for reader in readers):
            out.warning = (
                "Output stream stayed open after the command exited; a "
                "background process it started may still be running, so the "
                "captured output may be incomplete."
            )
    except KeyboardInterrupt:
        if not completed:
            out = _Outcome(interrupted=True)
            out.error_message = "Command was interrupted before it finished."
            _ignore_interrupt(
                _terminate_group,
                process,
                pgid,
                (signal.SIGINT, signal.SIGTERM, signal.SIGKILL),
            )
            out.exit_code = process.returncode
    finally:
        stop.set()
        for reader in readers:
            _ignore_interrupt(reader.join, _READER_JOIN_SECONDS)

    return out


def _terminal_size() -> "tuple[int, int]":
    """Return ``(rows, cols)`` of the user's terminal, falling back to 24x80.

    Programs size their output to the terminal they detect, so the captured
    command should see the same dimensions the user does rather than a fixed
    80 columns; otherwise wrapping in the stored preview differs from what the
    user saw live. Falls back when DebugBrief's own streams are not a terminal.
    """
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            size = os.get_terminal_size(stream.fileno())
        except (OSError, ValueError, AttributeError):
            continue
        if size.columns > 0 and size.lines > 0:
            return size.lines, size.columns
    return 24, 80


def _capture_via_pty(
    popen_args: Union[str, List[str]],
    command: str,
    cwd: Path,
    use_shell: bool,
    timeout_seconds: int,
    echo: bool,
    out_bounded: _BoundedText,
    err_bounded: _BoundedText,
) -> _Outcome:
    """Run under pseudo-terminals so output streams live. Raises
    :class:`_PtyUnavailable` when a pty cannot be allocated."""
    import fcntl
    import pty
    import struct
    import termios

    opened: List[int] = []
    try:
        out_master, out_slave = pty.openpty()
        opened += [out_master, out_slave]
        err_master, err_slave = pty.openpty()
        opened += [err_master, err_slave]
    except OSError as exc:
        for fd in opened:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise _PtyUnavailable(str(exc)) from exc

    rows, cols = _terminal_size()
    for slave in (out_slave, err_slave):
        try:
            attrs = termios.tcgetattr(slave)
            attrs[1] &= ~termios.ONLCR  # do not translate NL to CR-NL
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
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
        return _Outcome(error_message=_popen_error_message(exc, command))

    for fd in (out_slave, err_slave):
        with contextlib.suppress(OSError):
            os.close(fd)

    try:
        return _drive(
            process, process.pid, out_master, err_master,
            out_bounded, err_bounded, echo, timeout_seconds,
        )
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
) -> _Outcome:
    """Run with plain pipes (fallback when no pty is available)."""
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
        return _Outcome(error_message=_popen_error_message(exc, command))

    assert process.stdout is not None and process.stderr is not None
    try:
        return _drive(
            process, process.pid,
            process.stdout.fileno(), process.stderr.fileno(),
            out_bounded, err_bounded, echo, timeout_seconds,
        )
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
    output streams live (disable echo with ``echo=False``) and a timeout, a
    Ctrl-C, or a closed downstream pipe terminates the group. Output is retained
    through a bounded buffer. Pass ``redact=False`` to store output verbatim;
    ``force_verification`` marks an unrecognized command as a declared check.
    """
    started_at = now_iso8601()
    start_monotonic = time.monotonic()

    errored = False
    error_message: Optional[str] = None

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

    if errored:
        outcome = _Outcome(error_message=error_message)
    else:
        args = (
            popen_args, command, cwd, use_shell, timeout_seconds, echo,
            out_bounded, err_bounded,
        )
        try:
            outcome = _capture_via_pty(*args)
        except _PtyUnavailable:
            outcome = _capture_via_pipes(*args)
        errored = (
            outcome.error_message is not None
            and not outcome.timed_out
            and not outcome.interrupted
            and not outcome.broken_pipe
        )

    ended_at = now_iso8601()
    duration = round(time.monotonic() - start_monotonic, 3)

    # Output was cleaned (escapes and unsafe controls stripped, CR-LF normalized)
    # as it was read, so the bounded previews are already report-ready.
    stdout_preview, stdout_truncated = out_bounded.result()
    stderr_preview, stderr_truncated = err_bounded.result()

    classification = filters.classify_command(
        command=command,
        exit_code=outcome.exit_code,
        timed_out=outcome.timed_out,
        errored=errored,
        force_verification=force_verification,
        interrupted=outcome.interrupted,
        broken_pipe=outcome.broken_pipe,
        use_shell=use_shell,
    )

    # A recognized check run inside a shell pipeline is not treated as a
    # verification (its exit status is only the last stage's); say so plainly.
    warning = outcome.warning
    if filters.shell_pipeline_suppressed_check(command, use_shell):
        pipeline_warning = (
            "The command is a shell pipeline, so its exit status reflects only "
            "the last stage, not the check. It is recorded as a command but not "
            "treated as a verification. Run the check without a pipeline (or set "
            "the shell's pipefail) for a reliable pass/fail."
        )
        warning = f"{warning} {pipeline_warning}".strip() if warning else pipeline_warning

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
        exit_code=outcome.exit_code,
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
        timed_out=outcome.timed_out,
        errored=errored,
        error_message=outcome.error_message,
        interrupted=outcome.interrupted,
        broken_pipe=outcome.broken_pipe,
        warning=warning,
    )
