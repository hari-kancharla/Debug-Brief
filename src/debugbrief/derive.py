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
from typing import Dict, List, Optional

from .filters import is_non_verification_command
from .markdown import (
    code_span,
    inline_path,
    normalize_text,
)
from .models import (
    COMMAND_STATUS_PASSED,
    NON_SUCCESS_STATUSES,
    CommandClassification,
    CommandData,
    Session,
    effective_event_timestamp,
    valid_timestamp,
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
    timestamp_valid: bool
    status: str
    exit_code: Optional[int]
    duration_seconds: float
    is_test: bool
    is_verification: bool
    tool: Optional[str]
    stderr_preview: str
    stdout_preview: str
    changed_files: List[str]
    fingerprints: Dict[str, str]
    head_sha: Optional[str]
    redacted: bool
    invocation_cwd: Optional[str] = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False

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
        return (
            self.status in {COMMAND_STATUS_PASSED, *_FAIL_STATUSES}
            and (self.is_test or self.tool is not None)
        )


@dataclass
class RedToGreen:
    command: str
    failed_at: str
    passed_at: str
    window_seconds: float
    changed_files: List[str]
    invocation_cwd: Optional[str] = None
    commit_boundary: bool = False


@dataclass
class Derivation:
    one_liner: Optional[str] = None
    reproduce_command: Optional[str] = None
    verify_command: Optional[str] = None
    reproduce_invocation_cwd: Optional[str] = None
    verify_invocation_cwd: Optional[str] = None
    red_to_green: Optional[RedToGreen] = None
    observed_error: Optional[str] = None
    ruled_out: List[CommandRecord] = field(default_factory=list)
    redaction_applied: bool = False
    proof_state: str = "unverified"
    proof_basis: str = ""
    proof_command: Optional[str] = None
    invalid_verification_commands: List[str] = field(default_factory=list)
    command_records: List[CommandRecord] = field(default_factory=list)


def _seconds(timestamp: str) -> float:
    try:
        return parse_iso8601(timestamp).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0.0


def _records(session: Session) -> List[CommandRecord]:
    records: List[CommandRecord] = []
    for event in session.command_events():
        data = CommandData.from_dict(event.data)
        classification = data.classification
        if is_non_verification_command(data.command):
            classification = CommandClassification(status=classification.status)
        timestamp = effective_event_timestamp(event)
        records.append(
            CommandRecord(
                command=normalize_text(data.command),
                timestamp=timestamp,
                timestamp_valid=valid_timestamp(timestamp) is not None,
                status=classification.status,
                exit_code=data.exit_code,
                duration_seconds=data.duration_seconds,
                is_test=classification.is_test,
                is_verification=classification.is_verification,
                tool=classification.tool,
                stderr_preview=normalize_text(data.stderr_preview),
                stdout_preview=normalize_text(data.stdout_preview),
                changed_files=[normalize_text(path) for path in data.git_changed_files],
                fingerprints=dict(data.git_fingerprints),
                head_sha=data.git_head,
                redacted=data.redacted,
                invocation_cwd=data.invocation_cwd,
                stdout_truncated=data.stdout_truncated,
                stderr_truncated=data.stderr_truncated,
            )
        )
    records.sort(key=lambda r: _seconds(r.timestamp))
    return records


def _latest_verification_records(
    records: List[CommandRecord],
) -> List[CommandRecord]:
    latest: dict[tuple[str, Optional[str]], CommandRecord] = {}
    for record in records:
        if record.timestamp_valid and record.is_verification_candidate:
            latest[(record.command, record.invocation_cwd)] = record
    return list(latest.values())


def _session_span_seconds(session: Session) -> Optional[float]:
    """Total span from the first to the last recorded event, in seconds."""
    events = session.events if isinstance(session.events, list) else []
    stamps = [
        _seconds(effective_event_timestamp(e))
        for e in events
        if effective_event_timestamp(e)
    ]
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
        if rec.timestamp_valid and rec.is_verification_candidate and rec.failed:
            first_fail = rec
            break
    if first_fail is None:
        return None

    fail_seconds = _seconds(first_fail.timestamp)
    passed: Optional[CommandRecord] = None
    for rec in records:
        if (
            rec.timestamp_valid
            and rec.is_verification_candidate
            and rec.passed
            # Only the same check turning green counts as red-to-green: the same
            # command run in the same directory, not a different command or the
            # same command in another package of a monorepo.
            and rec.command == first_fail.command
            and rec.invocation_cwd == first_fail.invocation_cwd
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
    preexisting = session.git.initial_dirty
    for path in first_fail.changed_files:
        if path in preexisting:
            fingerprint = first_fail.fingerprints.get(path)
            if (
                fingerprint is not None
                and fingerprint != preexisting[path]
                # Legacy snapshots can lack the later content fingerprint.
                # With complete snapshots, compare against the failed check
                # below, rather than attributing a pre-failure edit to this window.
                and path not in passed.fingerprints
                and path not in changed
            ):
                changed.append(path)
    for rec in records:
        ts = _seconds(rec.timestamp)
        if fail_seconds < ts <= pass_seconds:
            for path in rec.changed_files:
                fingerprint = rec.fingerprints.get(path)
                failed_fingerprint = first_fail.fingerprints.get(path)
                if fingerprint is not None and failed_fingerprint is not None:
                    if fingerprint == failed_fingerprint:
                        continue
                elif path in preexisting and (
                    fingerprint is None or fingerprint == preexisting[path]
                ):
                    continue
                if path not in changed:
                    changed.append(path)

    return RedToGreen(
        command=passed.command,
        failed_at=first_fail.timestamp,
        passed_at=passed.timestamp,
        window_seconds=max(0.0, pass_seconds - fail_seconds),
        changed_files=sorted(changed),
        invocation_cwd=first_fail.invocation_cwd,
        commit_boundary=(
            (first_fail.head_sha is not None or passed.head_sha is not None)
            and first_fail.head_sha != passed.head_sha
        ),
    )


def _extract_observed_error(records: List[CommandRecord]) -> Optional[str]:
    """Quote a single, real error line from a failed command's output.

    Considers failing commands in priority order (the first failing verification
    command, then any other failing command) and, for each, prefers its stderr
    but falls back to its stdout. Many test tools (pytest among them) print the
    assertion and summary to stdout, so a higher-priority command whose error is
    only on stdout is still preferred over a lower-priority command's stderr.
    Only failed commands are considered; the text was already redacted at capture.
    """
    failing = [r for r in records if r.failed]
    candidates = [r for r in failing if r.is_verification_candidate]
    candidates += [r for r in failing if not r.is_verification_candidate]
    # Command priority dominates: take each candidate's stderr, then its stdout,
    # before moving to the next, so a lower-priority command's stderr never wins
    # over a higher-priority command's stdout.
    for rec in candidates:
        for source in ("stderr_preview", "stdout_preview"):
            text = getattr(rec, source)
            if not text.strip():
                continue
            line = _pick_error_line(text)
            if line:
                if len(line) > _OBSERVED_ERROR_LIMIT:
                    return line[:_OBSERVED_ERROR_LIMIT].rstrip() + " ..."
                return line
    return None


def _pick_error_line(stderr: str) -> Optional[str]:
    lines = [ln.strip() for ln in normalize_text(stderr).splitlines() if ln.strip()]
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
    clause = ", ".join(inline_path(path) for path in shown)
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

    latest = _latest_verification_records(records)
    latest_failed = [record for record in latest if record.failed]
    latest_passed = [record for record in latest if record.passed]
    invalid_verifications = [
        record
        for record in records
        if not record.timestamp_valid and record.is_verification_candidate
    ]

    if invalid_verifications:
        command = invalid_verifications[-1].command
        lead = f"Recorded {n} command {_attempts_word(n)}"
        if duration:
            lead += f" over {duration}"
        return f"{lead}; verification {code_span(command)} has no valid timestamp."

    if red_to_green is not None:
        later_failure = next(
            (
                record
                for record in latest_failed
                if record.command == red_to_green.command
                and record.invocation_cwd == red_to_green.invocation_cwd
                and _seconds(record.timestamp) > _seconds(red_to_green.passed_at)
            ),
            None,
        )
        if later_failure is None:
            parts = [f"Failing check {code_span(red_to_green.command)} passed"]
            if latest_failed:
                parts.append("but another verification is still failing")
        else:
            parts = [
                f"Check {code_span(red_to_green.command)} turned green, but the "
                "latest recorded run still failed"
            ]
        parts[-1] += f"; {n} command{'' if n == 1 else 's'} recorded in the session"
        if duration:
            parts.append(f"over {duration}")
        sentence = " ".join(parts)
        if files:
            sentence += f", changes touched {files}"
        return sentence + "."

    if latest_passed:
        cmd = latest_passed[0].command
        parts = [f"Verification {code_span(cmd)} passed"]
        if latest_failed:
            parts.append("but another verification is still failing")
        parts[-1] += f"; {n} command{'' if n == 1 else 's'} recorded in the session"
        if duration:
            parts.append(f"over {duration}")
        sentence = " ".join(parts)
        if files:
            sentence += f", changes touched {files}"
        return sentence + "."

    if latest_failed:
        cmd = latest_failed[0].command
        lead = f"Recorded {n} command {_attempts_word(n)}"
        if duration:
            lead += f" over {duration}"
        if any(record.passed for record in records):
            return f"{lead}; verification {code_span(cmd)} is still failing after an earlier pass."
        return f"{lead}; verification {code_span(cmd)} failed and none passed."

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


def _proof_state(
    records: List[CommandRecord], red_to_green: Optional[RedToGreen]
) -> tuple[str, str, Optional[str]]:
    current = _latest_verification_records(records)
    invalid = next(
        (
            record
            for record in records
            if record.is_verification_candidate and not record.timestamp_valid
        ),
        None,
    )
    if invalid is not None:
        return (
            "unverified",
            f"{invalid.command} has no valid recorded timestamp",
            invalid.command,
        )
    failed = next((record for record in current if record.failed), None)
    if failed is not None:
        return (
            "unverified",
            f"{failed.command} is still failing in the latest recorded run",
            failed.command,
        )
    passed = next((record for record in current if record.passed), None)
    if passed is not None:
        if red_to_green is not None and passed.command == red_to_green.command:
            return (
                "verified",
                "the same recorded check failed and later passed",
                passed.command,
            )
        return "verified", f"{passed.command} exited 0", passed.command
    return "unverified", "no verification command passed", None


def next_step_notes(session: Session) -> List[str]:
    """Return recorded notes that read as forward-looking next steps.

    Used by the handoff report so its next-steps section is drawn only from what
    the human actually wrote, never inferred.
    """
    out: List[str] = []
    for event in session.note_events():
        data = event.data if isinstance(event.data, dict) else {}
        text = normalize_text(data.get("text", "")).strip()
        if not text:
            continue
        if _NEXT_STEP_RE.search(text):
            out.append(text)
    return out


def derive(session: Session) -> Derivation:
    records = _records(session)
    red_to_green = _detect_red_to_green(session, records)

    reproduce_record = next(
        (
            r
            for r in reversed(records)
            if r.timestamp_valid and r.is_verification_candidate and r.failed
        ),
        None,
    )
    verify_record = next(
        (
            record
            for record in reversed(records)
            if red_to_green is not None
            and record.command == red_to_green.command
            and record.invocation_cwd == red_to_green.invocation_cwd
            and record.timestamp_valid
            and record.passed
        ),
        None,
    )
    if verify_record is None:
        verify_record = next(
            (
                r
                for r in reversed(records)
                if r.timestamp_valid and r.is_verification_candidate and r.passed
            ),
            None,
        )
    verify = verify_record.command if verify_record is not None else None

    redaction_applied = any(
        isinstance(event.data, dict) and bool(event.data.get("redacted"))
        for event in session.events
        if event.type in {"note", "warning"}
    )
    proof_state, proof_basis, proof_command = _proof_state(records, red_to_green)
    invalid_verification_commands = [
        record.command
        for record in records
        if record.is_verification_candidate and not record.timestamp_valid
    ]

    return Derivation(
        one_liner=_build_one_liner(session, records, red_to_green),
        reproduce_command=reproduce_record.command if reproduce_record is not None else None,
        verify_command=verify,
        reproduce_invocation_cwd=(
            reproduce_record.invocation_cwd if reproduce_record is not None else None
        ),
        verify_invocation_cwd=verify_record.invocation_cwd if verify_record is not None else None,
        red_to_green=red_to_green,
        observed_error=_extract_observed_error(records),
        ruled_out=[r for r in records if r.failed],
        redaction_applied=any(r.redacted for r in records) or redaction_applied,
        proof_state=proof_state,
        proof_basis=proof_basis,
        proof_command=proof_command,
        invalid_verification_commands=invalid_verification_commands,
        command_records=records,
    )
