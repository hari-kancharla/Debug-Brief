"""Optional per-project configuration from ``.debugbrief.toml``.

Parsed with the standard-library ``tomllib`` on Python 3.11+, and with the
``tomli`` backport on Python 3.9/3.10 (DebugBrief's one conditional runtime
dependency). Behavior is therefore identical on every supported Python: a
malformed file is ignored as a whole rather than partially applied, and a
missing or unreadable file never raises, so configuration can never break a
command. Only top-level keys are read:

    default_mode    = "pr" | "handoff" | "incident"   # default for end/preview
    timeout_seconds = <positive integer>              # default for run/redo
    detail          = "full" | "compact"              # default report verbosity
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import is_regular_file, open_regular_text

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.9/3.10 via the tomli backport
    import tomli as tomllib  # type: ignore[import-not-found]

CONFIG_FILENAME = ".debugbrief.toml"

_VALID_MODES = ("pr", "handoff", "incident")
_VALID_DETAIL = ("full", "compact")


def load_config(project_root: Path) -> Dict[str, Any]:
    """Return the validated config defaults for ``project_root``.

    Empty when the file is missing, unreadable, or malformed; a malformed file is
    never partially applied.
    """
    data = _read(project_root)
    return _coerce(data) if data is not None else {}


def parse_error(project_root: Path) -> Optional[str]:
    """Return a short reason if ``.debugbrief.toml`` exists but cannot be parsed.

    Used by ``doctor`` to flag a malformed config. The message names the file and
    the kind of failure but never quotes its contents, which could hold secrets.
    """
    path = Path(project_root) / CONFIG_FILENAME
    if not path.exists():
        return None
    if not is_regular_file(path):
        # A symlinked config would be followed and a FIFO would block the read.
        return f"{CONFIG_FILENAME} is not a regular file (symlink or special) and was ignored"
    try:
        with open_regular_text(path) as handle:
            text = handle.read()
    except OSError:
        return None
    except UnicodeError:
        # Invalid UTF-8 surfaces while reading, before tomllib sees it.
        return f"{CONFIG_FILENAME} is not valid UTF-8 and was ignored"
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return f"{CONFIG_FILENAME} is not valid TOML and was ignored"
    except (UnicodeError, ValueError):
        return f"{CONFIG_FILENAME} could not be parsed and was ignored"
    return None


def _read(project_root: Path) -> Optional[Dict[str, Any]]:
    """Parse ``.debugbrief.toml`` into a dict, or None if absent/unsafe/unreadable/bad."""
    path = Path(project_root) / CONFIG_FILENAME
    # is_regular_file (lstat) is the gate: a symlinked config is not followed and
    # a FIFO does not block load_config, which runs on every command.
    if not is_regular_file(path):
        return None
    try:
        with open_regular_text(path) as handle:
            text = handle.read()
    except (OSError, UnicodeError):
        # Unreadable or not valid UTF-8 (raised while reading): ignore.
        return None
    try:
        parsed = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeError, ValueError):
        # Malformed: ignore the whole file rather than apply part of it.
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce(data: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only recognized top-level keys with valid values; ignore the rest.

    Keys nested under a ``[section]`` are not top-level, so ``data.get`` never
    returns them: a sectioned ``timeout_seconds`` cannot alter the real timeout.
    """
    out: Dict[str, Any] = {}
    if not isinstance(data, dict):
        return out

    mode = data.get("default_mode")
    if isinstance(mode, str) and mode in _VALID_MODES:
        out["default_mode"] = mode

    timeout = data.get("timeout_seconds")
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        out["timeout_seconds"] = timeout

    detail = data.get("detail")
    if isinstance(detail, str) and detail in _VALID_DETAIL:
        out["detail"] = detail

    return out
