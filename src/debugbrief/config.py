"""Optional per-project configuration from ``.debugbrief.toml``.

Zero-dependency and best effort. Parsed with the standard-library ``tomllib`` on
Python 3.11+, falling back to a tiny flat-key parser (also used on older
versions) when ``tomllib`` reports a syntax error, so behavior is the same on
every supported Python: recognized keys are read and lines that cannot be parsed
are skipped. A missing or unreadable file never raises, so configuration can
never break a command. Supported keys:

    default_mode    = "pr" | "handoff" | "incident"   # default for end/preview
    timeout_seconds = <positive integer>              # default for run/redo
    detail          = "full" | "compact"              # default report verbosity
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

CONFIG_FILENAME = ".debugbrief.toml"

_VALID_MODES = ("pr", "handoff", "incident")
_VALID_DETAIL = ("full", "compact")


def load_config(project_root: Path) -> Dict[str, Any]:
    """Return the validated config defaults for ``project_root`` (empty if none)."""
    path = Path(project_root) / CONFIG_FILENAME
    try:
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return _coerce(_parse(text))


def _parse(text: str) -> Dict[str, Any]:
    try:
        import tomllib  # type: ignore[import-not-found]  # Python 3.11+
    except ModuleNotFoundError:
        return _parse_flat(text)
    try:
        return tomllib.loads(text)
    except Exception:
        # A TOML syntax error falls back to the lenient flat parser, so behavior
        # is the same on every supported Python (recognized key = value lines are
        # read, unparseable lines are skipped) rather than differing by version.
        return _parse_flat(text)


def _parse_flat(text: str) -> Dict[str, Any]:
    """Fallback for Python < 3.11: top-level ``key = value`` scalars only.

    Handles quoted strings, integers, and booleans. Comments are skipped. A
    ``[section]`` / ``[[array]]`` header stops parsing entirely: in TOML every
    key after the first table header belongs to that section, so no later key is
    top-level. Stopping there keeps a sectioned key (``[tool.other]`` then
    ``timeout_seconds = 1``) from being hoisted to the top level and silently
    applied, which would otherwise diverge from how ``tomllib`` scopes it.
    """
    result: Dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        key, sep, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not sep or not key or not value:
            continue
        result[key] = _scalar(value)
    return result


def _scalar(value: str) -> Any:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if value.lstrip("-").isdigit():
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _coerce(data: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only recognized keys with valid values; ignore everything else."""
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
