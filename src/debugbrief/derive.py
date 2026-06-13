"""Deterministic derivations shared by every report mode.

Everything here is computed only from recorded events. Nothing asserts a cause,
and no value is invented: anything the data cannot support is left as ``None``
or an empty list so the reporters can omit the corresponding section.

The reporters used to restate counts and echo notes. These derivations instead
reconstruct the shape of the investigation (what failed, what then passed, what
changed in between, what was ruled out) strictly from evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .models import (
    COMMAND_STATUS_PASSED,
    NON_SUCCESS_STATUSES,
    CommandData,
    Session,
)
from .utils import human_duration, parse_iso8601

_FAIL_STATUSES = NON_SUCCESS_STATUSES

# Cap how much of an error line we quote verbatim.
_OBSERVED_ERROR_LIMIT = 300

# Phrases that mark a recorded note as forward-looking (used for handoff steps).
# Matched on word boundaries so "retry" does not look like "try".
_NEXT_STEP_HINTS = (
    "next",
    "todo",
    "to do",
    "try",
    "should",
    "need to",
    "needs to",
    "follow up",
    "follow-up",
    "investigate",
    "check",
)
_NEXT_STEP_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in _NEXT_STEP_HINTS) + r")\b",
    re.IGNORECASE,
)


@dataclass
class CommandRecord:
    """A single command event, flattened for derivation and reporting."""

    command: str
    timestamp: str
    status: str
    exit_code: Optional[int]
    duration_seconds: float
    is_test: bool
    is_verification: bool
    tool: Optional[str]
    stderr_preview: str
    changed_files: List[str]
    head_sha: Optional[str]
    redacted: bool

    @property
    def failed(self) -> bool:
        return self.status in _FAIL_STATUSES

    @property
    def passed(self) -> bool:
        return self.status == COMMAND_STATUS_PASSED

    @property
    def is_verification_candidate(self) -> bool:
        """A recognized test/build/lint/typecheck command, pass or fail.

        ``is_verification`` is only true when such a command passed; this stays
        true regardless of outcome so a failing check still counts.
        """
        return self.is_test or self.tool is not None


@dataclass
class RedToGreen:
    command: str
    failed_at: str
    passed_at: str
    window_seconds: float
    changed_files: List[str]


@dataclass
class Derivation:
    one_liner: Optional[str] = None
    reproduce_command: Optional[str] = None
    verify_command: Optional[str] = None
    red_to_green: Optional[RedToGreen] = None
    observed_error: Optional[str] = None
    ruled_out: List[CommandRecord] = field(default_factory=list)
    redaction_applied: bool = False
    command_records: List[CommandRecord] = field(default_factory=list)


def _seconds(timestamp: str) -> float:
    try:
        return parse_iso8601(timestamp).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _records(session: Session) -> List[CommandRecord]:
    records: List[CommandRecord] = []
    for event in session.command_events():
        data = CommandData.from_dict(event.data)
        records.append(
            CommandRecord(
                command=data.command,
                timestamp=event.timestamp,
                status=data.classification.status,
                exit_code=data.exit_code,
                duration_seconds=data.duration_seconds,
                is_test=data.classification.is_test,
                is_verification=data.classification.is_verification,
                tool=data.classification.tool,
                stderr_preview=data.stderr_preview,
                changed_files=list(data.git_changed_files),
                head_sha=data.git_head,
                redacted=data.redacted,
            )
        )
    records.sort(key=lambda r: _seconds(r.timestamp))
    return records


def _session_span_seconds(session: Session) -> Optional[float]:
    """Total span from the first to the last recorded event, in seconds."""
    stamps = [_seconds(e.timestamp) for e in session.events if e.timestamp]
    stamps = [s for s in stamps if s > 0]
    if len(stamps) < 2:
        return None
    span = max(stamps) - min(stamps)
    return span if span > 0 else None


def _detect_red_to_green(
    session: Session, records: List[CommandRecord]
) -> Optional[RedToGreen]:
    first_fail: Optional[CommandRecord] = None
    for rec in records:
        if rec.is_verification_candidate and rec.failed:
            first_fail = rec
            break
    if first_fail is None:
        return None

    fail_seconds = _seconds(first_fail.timestamp)
    passed: Optional[CommandRecord] = None
    for rec in records:
        if (
            rec.is_verification_candidate
            and rec.passed
            and _seconds(rec.timestamp) > fail_seconds
        ):
            passed = rec
            break
    if passed is None:
        return None

    # Correlate file changes across the window from per-event snapshots. Only
    # meaningful inside a repo; reported as correlation, never as cause.
    if not session.git.is_repo:
        return None
    pass_seconds = _seconds(passed.timestamp)
    changed: List[str] = []
    for rec in records:
        ts = _seconds(rec.timestamp)
        if fail_seconds <= ts <= pass_seconds:
            for path in rec.changed_files:
                if path not in changed:
                    changed.append(path)

    return RedToGreen(
        command=passed.command,
        failed_at=first_fail.timestamp,
        passed_at=passed.timestamp,
        window_seconds=max(0.0, pass_seconds - fail_seconds),
        changed_files=sorted(changed),
    )


def _extract_observed_error(records: List[CommandRecord]) -> Optional[str]:
    """Quote a single, real error line from a failed command's stderr.

    Prefers the first failing verification command, then any failing command.
    The text was already redacted at capture time.
    """
    failing = [r for r in records if r.failed and r.stderr_preview.strip()]
    candidates = [r for r in failing if r.is_verification_candidate]
    candidates += [r for r in failing if not r.is_verification_candidate]
    for rec in candidates:
        line = _pick_error_line(rec.stderr_preview)
        if line:
            if len(line) > _OBSERVED_ERROR_LIMIT:
                return line[:_OBSERVED_ERROR_LIMIT].rstrip() + " ..."
            return line
    return None


def _pick_error_line(stderr: str) -> Optional[str]:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    if not lines:
        return None
    # Prefer a line that looks like an assertion or error message.
    for ln in reversed(lines):
        lowered = ln.lower()
        if (
            "error" in lowered
            or "assert" in lowered
            or "exception" in lowered
            or "traceback" in lowered
        ):
            return ln
    # Otherwise the last non-empty line.
    return lines[-1]


def _files_clause(session: Session, limit: int = 3) -> Optional[str]:
    if not session.git.is_repo:
        return None
    files = list(session.summary.modified_files)
    if not files:
        return None
    shown = files[:limit]
    clause = ", ".join(shown)
    extra = len(files) - len(shown)
    if extra > 0:
        clause += f", and {extra} more"
    return clause


def _attempts_word(n: int) -> str:
    return "attempt" if n == 1 else "attempts"


def _build_one_liner(
    session: Session,
    records: List[CommandRecord],
    red_to_green: Optional[RedToGreen],
) -> Optional[str]:
    n = len(records)
    span = _session_span_seconds(session)
    duration = human_duration(span) if span is not None else None
    files = _files_clause(session)

    if red_to_green is not None:
        parts = [f"Failing check `{red_to_green.command}` passed"]
        parts.append(f"after {n} {_attempts_word(n)}")
        if duration:
            parts.append(f"over {duration}")
        sentence = " ".join(parts)
        if files:
            sentence += f", changes touched {files}"
        return sentence + "."

    passed_verifications = [r for r in records if r.is_verification and r.passed]
    failed_candidates = [r for r in records if r.is_verification_candidate and r.failed]

    if passed_verifications:
        cmd = passed_verifications[0].command
        parts = [f"Verification `{cmd}` passed"]
        parts.append(f"after {n} {_attempts_word(n)}")
        if duration:
            parts.append(f"over {duration}")
        sentence = " ".join(parts)
        if files:
            sentence += f", changes touched {files}"
        return sentence + "."

    if failed_candidates:
        cmd = failed_candidates[0].command
        lead = f"Recorded {n} command {_attempts_word(n)}"
        if duration:
            lead += f" over {duration}"
        return f"{lead}; verification `{cmd}` failed and none passed."

    if n > 0:
        lead = f"Recorded {n} command {_attempts_word(n)}"
        if duration:
            lead += f" over {duration}"
        return f"{lead}; no verification commands were run."

    notes = len(session.note_events())
    if notes:
        word = "note" if notes == 1 else "notes"
        lead = f"Recorded {notes} {word}"
        if duration:
            lead += f" over {duration}"
        return f"{lead}; no commands were run."

    return None


def next_step_notes(session: Session) -> List[str]:
    """Return recorded notes that read as forward-looking next steps.

    Used by the handoff report so its next-steps section is drawn only from what
    the human actually wrote, never inferred.
    """
    out: List[str] = []
    for event in session.note_events():
        text = (event.data or {}).get("text", "").strip()
        if not text:
            continue
        if _NEXT_STEP_RE.search(text):
            out.append(text)
    return out


def derive(session: Session) -> Derivation:
    records = _records(session)
    red_to_green = _detect_red_to_green(session, records)

    reproduce = next(
        (r.command for r in records if r.is_verification_candidate and r.failed), None
    )
    verify = next(
        (r.command for r in records if r.is_verification_candidate and r.passed), None
    )

    notes_redacted = any(
        bool((e.data or {}).get("redacted")) for e in session.note_events()
    )

    return Derivation(
        one_liner=_build_one_liner(session, records, red_to_green),
        reproduce_command=reproduce,
        verify_command=verify,
        red_to_green=red_to_green,
        observed_error=_extract_observed_error(records),
        ruled_out=[r for r in records if r.failed],
        redaction_applied=any(r.redacted for r in records) or notes_redacted,
        command_records=records,
    )
