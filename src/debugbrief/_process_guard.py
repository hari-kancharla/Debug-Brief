"""Private POSIX command supervisor, also executable as an isolated script.

The recorder alone holds the write end of a lifetime pipe. EOF therefore means
the recorder died, even on SIGKILL. The supervisor is the command's session and
process-group leader: it stays alive until cleanup, so its group id cannot be
reused while it signals the group. Commands never inherit the lifetime pipe.
No fork hooks, PID scanning, external services, or runtime dependencies are used.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Union


class SupervisedProcess:
    """Popen-like handle whose result is the actual command's exit status."""

    def __init__(
        self, args: Union[str, List[str]], cwd: Path, shell: bool,
        stdout: Any, stderr: Any, pass_fds: "tuple[int, ...]" = (),
    ) -> None:
        self.returncode: Optional[int] = None
        self.error_message: Optional[str] = None
        self._received = False
        self._buffer = b""
        self._control_r, self._control_w = os.pipe()
        try:
            self._status_r, self._status_w = os.pipe()
        except OSError:
            os.close(self._control_r)
            os.close(self._control_w)
            raise
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-I", "-S", str(Path(__file__).resolve()),
                 str(self._control_r), str(self._status_w)],
                stdout=stdout, stderr=stderr, start_new_session=True,
                close_fds=True, pass_fds=(*pass_fds, self._control_r, self._status_w),
            )
            self.pid = self._process.pid
            payload = json.dumps({
                "args": args, "cwd": str(cwd), "shell": shell, "pass_fds": pass_fds,
            }).encode() + b"\n"
            while payload:
                written = os.write(self._control_w, payload)
                payload = payload[written:]
        except BaseException:
            os.close(self._control_w)
            if hasattr(self, "_process"):
                self._process.wait(timeout=5)
            os.close(self._status_r)
            raise
        finally:
            os.close(self._control_r)
            os.close(self._status_w)
        self.stdout = self._process.stdout
        self.stderr = self._process.stderr

    def wait(self, timeout: Optional[float] = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._received:
            # Also reap an unexpectedly killed supervisor; never invent a pass.
            try:
                guard_code = self._process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                guard_code = None
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self._status_r], [], [], remaining)
            if not ready:
                raise subprocess.TimeoutExpired("supervised command", timeout or 0)
            data = os.read(self._status_r, 65536)
            self._buffer += data
            if b"\n" in self._buffer:
                result = json.loads(self._buffer.split(b"\n", 1)[0])
                self.returncode = result["exit_code"]
                self.error_message = result.get("error")
                self._received = True
            elif not data:
                self.returncode = guard_code if guard_code is not None else self._process.wait()
                self.error_message = (
                    "Command supervisor stopped before reporting the command outcome."
                )
                self._received = True
        # An execution error is carried separately, never persisted as exit 0.
        return self.returncode if self.returncode is not None else 1

    def poll(self) -> Optional[int]:
        try:
            return self.wait(timeout=0)
        except subprocess.TimeoutExpired:
            return None

    def send_signal(self, sig: int) -> None:
        os.killpg(self.pid, sig)

    def finish(self) -> None:
        """Release the supervisor only after output has drained; reap it."""
        if self._control_w >= 0:
            with contextlib.suppress(OSError):
                os.write(self._control_w, b"D")
                os.close(self._control_w)
            self._control_w = -1
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # It is still our live group leader, so this cannot hit a reused id.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pid, signal.SIGKILL)
            self._process.wait(timeout=3)
        finally:
            if self._status_r >= 0:
                os.close(self._status_r)
                self._status_r = -1


def _cleanup(normal: bool) -> None:
    """Signal our own anchored group, including TERM-resistant descendants."""
    os.killpg(os.getpgrp(), signal.SIGTERM)
    # A hard crash gets a longer grace period. A completed command has already
    # exited and drained its output; only background descendants remain.
    time.sleep(0.1 if normal else 0.5)
    os.killpg(os.getpgrp(), signal.SIGKILL)


def _main(control: int, status: int) -> None:
    if os.getpid() != os.getpgrp() or os.getpid() != os.getsid(0):
        return  # Never signal a group we did not create and anchor ourselves.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
        signal.signal(sig, lambda *_: None)
    # EOF during startup means no command may be launched. Read only the config
    # line, leaving the following completion byte in the unbuffered lifetime fd.
    config = bytearray()
    while True:
        chunk = os.read(control, 1)
        if not chunk:
            return
        if chunk == b"\n":
            break
        config.extend(chunk)
    spec = json.loads(config)
    if select.select([control], [], [], 0)[0] and not os.read(control, 1):
        return
    try:
        child = subprocess.Popen(
            spec["args"], cwd=spec["cwd"], shell=spec["shell"],
            close_fds=True, pass_fds=tuple(spec["pass_fds"]),
        )
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            error = f"Command not found: {exc.filename or spec['args']}"
        elif isinstance(exc, PermissionError):
            error = f"Permission denied: {exc.filename or spec['args']}"
        else:
            error = f"Failed to execute command: {exc}"
        os.write(status, json.dumps({"exit_code": None, "error": error}).encode() + b"\n")
        return
    try:
        # Only the real command should keep captured streams open.
        os.close(1)
        os.close(2)
        reported = False
        while True:
            if not reported and child.poll() is not None:
                with contextlib.suppress(BrokenPipeError):
                    os.write(status, json.dumps({"exit_code": child.returncode}).encode() + b"\n")
                reported = True
            if select.select([control], [], [], 0.05)[0]:
                completion = os.read(control, 1)
                _cleanup(normal=completion == b"D")
    finally:
        # An internal exception must not abandon a command we already started.
        _cleanup(normal=False)


if __name__ == "__main__":
    try:
        _main(int(sys.argv[1]), int(sys.argv[2]))
    finally:
        os._exit(0)
