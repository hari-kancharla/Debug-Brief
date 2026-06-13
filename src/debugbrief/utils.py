"""Small shared helpers: timestamps, output truncation, and atomic JSON I/O.

These helpers are deliberately dependency-free and side-effect minimal so the
rest of the package can rely on consistent, testable behavior.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple

# Default preview limits for captured command output. We store previews, not
# unbounded logs, so a single noisy command can never balloon a session file.
DEFAULT_STDOUT_PREVIEW_LIMIT = 4000
DEFAULT_STDERR_PREVIEW_LIMIT = 4000


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_iso8601(moment: datetime) -> str:
    """Serialize a datetime to an ISO8601 UTC string ending in 'Z'.

    Naive datetimes are assumed to already be UTC.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    # Use millisecond precision; drop the '+00:00' offset in favor of 'Z'.
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso8601() -> str:
    """Convenience: current UTC time as an ISO8601 string."""
    return to_iso8601(utc_now())


def parse_iso8601(value: str) -> datetime:
    """Parse an ISO8601 string (possibly ending in 'Z') into a UTC datetime."""
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def human_duration(seconds: float) -> str:
    """Render a duration in seconds as a compact ``1h 2m 3s`` style string."""
    total = int(round(seconds))
    if total < 0:
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def truncate_text(text: str, limit: int) -> Tuple[str, bool]:
    """Truncate ``text`` to ``limit`` characters, keeping the head and tail.

    Returns a tuple of (possibly truncated text, was_truncated). A ``limit`` of
    zero or negative is treated as "no limit".

    When the text is longer than ``limit`` we keep a small head and a larger
    tail with an elision marker in between. The decisive output of a debugging
    run (tracebacks, assertions, the final build error) lands at the end, so the
    tail gets the larger share: the head is the first ``limit // 3`` characters
    and the tail is the remaining budget. The kept original content totals
    ``limit`` characters; the marker is added on top.
    """
    if text is None:
        return "", False
    if limit is None or limit <= 0:
        return text, False
    if len(text) <= limit:
        return text, False
    head_len = limit // 3
    tail_len = limit - head_len
    omitted = len(text) - limit
    marker = f"\n... [{omitted} characters omitted] ...\n"
    head = text[:head_len]
    tail = text[len(text) - tail_len:]
    return head + marker + tail, True


def atomic_write_json(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` atomically.

    The data is written to a temporary file in the same directory and then
    renamed into place, so a crash mid-write cannot corrupt an existing file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        # Best-effort cleanup of the temp file on any failure.
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Any:
    """Read and parse JSON from ``path``."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, owner read/write only.

    The text is written to a temporary file, flushed and fsynced, then renamed
    into place, so a crash or disk-full mid-write cannot leave a half-written
    report. Mode is restricted to 0600 so a generated brief, which can quote
    command output, is not left world-readable by the user's umask.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
        # Best effort on exotic filesystems that do not support chmod.
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        raise


def is_supported_platform() -> bool:
    """Return True on Unix-like platforms (Linux, macOS, BSD).

    V1 explicitly does not support Windows / PowerShell.
    """
    return os.name == "posix"


def eprint(*args: Any, **kwargs: Any) -> None:
    """Print to stderr."""
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)
