"""Conservative, best-effort secret redaction applied at capture time.

Reports are pasted into pull requests and handoff docs, so captured output and
command text are scrubbed before anything is written to disk. This is pure
standard-library regex with no dependency.

Scope is deliberately limited: it masks common, recognizable secret shapes and
nothing more. It will miss secrets that do not match a known pattern, so it is
never presented as a guarantee. When a value is masked it is replaced with the
literal placeholder ``[redacted]``.
"""

from __future__ import annotations

import re
from typing import Callable, List, Tuple, Union

PLACEHOLDER = "[redacted]"

# Tokens that mark a key as sensitive in a key/value pair. These must appear as a
# whole segment of the key name (delimited by the start/end of the key or by a
# ``_``/``-``/``.`` separator) so embedded substrings like the "key" in "monkey"
# or the "api" in "rapid_mode" are not mistaken for secrets.
_SENSITIVE_KEY = (
    r"(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|credential|api|key)"
)

# A key name is sensitive when one of its segments is a sensitive token. The
# token must be flanked by a non-alphanumeric boundary on both sides (start/end
# of the key or a separator), while still allowing other segments around it.
_SENSITIVE_KEY_NAME = (
    r"(?P<key>[A-Za-z0-9_.\-]*?(?<![A-Za-z0-9])"
    + _SENSITIVE_KEY
    + r"(?![A-Za-z0-9])[A-Za-z0-9_.\-]*\s*[:=]\s*)"
)

# A redaction replacement is either a literal string or a match-to-string
# callback, matching the two forms accepted by ``re.Pattern.subn``.
_Replacement = Union[str, Callable[["re.Match[str]"], str]]


def _kv_repl(match: "re.Match[str]") -> str:
    # Preserve the key, separator and any surrounding quotes; mask the value.
    open_quote = match.group("q") or ""
    return f"{match.group('key')}{open_quote}{PLACEHOLDER}{open_quote}"


# Order matters: multi-line and structured shapes first, broad key/value last.
_RULES: List[Tuple["re.Pattern[str]", _Replacement]] = [
    # PEM-style private key blocks (any key type), including the body.
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        PLACEHOLDER,
    ),
    # Connection strings: scheme://user:password@host -> mask only the password.
    (
        re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+):[^\s:/@]+@"),
        r"\1:" + PLACEHOLDER + "@",
    ),
    # Authorization headers (value may be a Bearer/Basic token).
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?\S+"),
        r"\1" + PLACEHOLDER,
    ),
    # Standalone bearer tokens.
    (
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),
        "Bearer " + PLACEHOLDER,
    ),
    # Provider key shapes.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), PLACEHOLDER),  # OpenAI-style
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), PLACEHOLDER),  # AWS access key id
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), PLACEHOLDER),  # GitHub tokens
    # Generic key/value pairs whose key name looks sensitive.
    (
        re.compile(
            r"(?i)" + _SENSITIVE_KEY_NAME + r"(?P<q>[\"'])?(?P<val>[^\s\"',;]+)(?P=q)?"
        ),
        _kv_repl,
    ),
]


def redact_text(text: str) -> Tuple[str, int]:
    """Return ``(redacted_text, count)`` where ``count`` is the number of masks.

    Best effort: applies each rule in turn. A ``count`` greater than zero means
    at least one secret-shaped value was replaced.
    """
    if not text:
        return text, 0
    total = 0
    for pattern, repl in _RULES:
        text, n = pattern.subn(repl, text)
        total += n
    return text, total
