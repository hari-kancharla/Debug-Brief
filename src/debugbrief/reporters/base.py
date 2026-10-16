"""Shared building blocks for markdown report generation.

A :class:`ReportContext` is computed once from a finalized session and contains
only deterministic, evidence-backed data. Reporters consume it to render their
mode-specific markdown. No reporter invents root causes, intent, or results.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..derive import Derivation, derive
from ..filters import ReportCommand, build_report_commands
from ..markdown import (
    code_span,
    fenced_code,
    inline_text,
    normalize_free_text,
    normalize_text,
)
from ..models import (
    COMMAND_STATUS_BROKEN_PIPE,
    COMMAND_STATUS_ERROR,
    COMMAND_STATUS_INTERRUPTED,
    COMMAND_STATUS_PASSED,
    COMMAND_STATUS_TIMED_OUT,
    CommandData,
    Event,
    EventType,
    Session,
    effective_event_timestamp,
    valid_timestamp,
)
from ..utils import human_duration, parse_iso8601


@dataclass
class TimelineEntry:
    timestamp: str
    kind: str
    text: str
    plain_text: Optional[str] = None
    status: Optional[str] = None
    working_directory: Optional[str] = None


@dataclass
class ReportContext:
    session: Session
    report_commands: List[ReportCommand] = field(default_factory=list)
    failed_commands: List[ReportCommand] = field(default_factory=list)
    currently_failing: List[ReportCommand] = field(default_factory=list)
    verification_commands: List[ReportCommand] = field(default_factory=list)
    test_commands: List[ReportCommand] = field(default_factory=list)
    notes: List[Tuple[str, str]] = field(default_factory=list)
    timeline: List[TimelineEntry] = field(default_factory=list)
    derivation: Derivation = field(default_factory=Derivation)


def _event_data(event: Any) -> Dict[str, Any]:
    return event.data if isinstance(event.data, dict) else {}


def _events(session: Session) -> List[Any]:
    return session.events if isinstance(session.events, list) else []


def _short_time(iso_timestamp: Optional[str]) -> str:
    if not iso_timestamp:
        return "unknown time"
    try:
        moment = parse_iso8601(iso_timestamp)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return inline_text(iso_timestamp)
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _clock(iso_timestamp: Optional[str]) -> str:
    if not iso_timestamp:
        return "--:--:--"
    try:
        moment = parse_iso8601(iso_timestamp)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return inline_text(iso_timestamp)
    return moment.strftime("%H:%M:%S")


def status_label(status: str) -> str:
    text = normalize_free_text(status)
    return {
        COMMAND_STATUS_PASSED: "passed",
        "failed": "failed",
        COMMAND_STATUS_TIMED_OUT: "timed out",
        COMMAND_STATUS_ERROR: "did not run",
        COMMAND_STATUS_INTERRUPTED: "interrupted",
        COMMAND_STATUS_BROKEN_PIPE: "broken pipe",
    }.get(text, inline_text(text))


def build_context(session: Session) -> ReportContext:
    command_events = session.command_events()
    report_commands = build_report_commands(command_events)

    failed = [rc for rc in report_commands if rc.failed]

    # Only the latest outcome for each check can support a current verification
    # claim. A historical pass remains visible in the raw command list, but must
    # not make a later failure look verified.
    latest_by_check: Dict[Tuple[str, Optional[str]], ReportCommand] = {}
    for rc in report_commands:
        if valid_timestamp(rc.last_timestamp) is None:
            continue
        key = (rc.command, rc.invocation_cwd)
        prev = latest_by_check.get(key)
        if prev is None or _safe_seconds(rc.last_timestamp) >= _safe_seconds(
            prev.last_timestamp
        ):
            latest_by_check[key] = rc
    verification = [
        rc
        for rc in report_commands
        if rc.is_verification
        and latest_by_check.get((rc.command, rc.invocation_cwd)) is rc
    ]
    tests = [rc for rc in report_commands if rc.is_test]
    currently_failing = [rc for rc in latest_by_check.values() if rc.failed]

    notes: List[Tuple[str, str]] = []
    for event in session.note_events():
        text = normalize_text(_event_data(event).get("text", "")).strip()
        if text:
            notes.append((event.timestamp, text))

    timeline = _build_timeline(session)

    return ReportContext(
        session=session,
        report_commands=report_commands,
        failed_commands=failed,
        currently_failing=currently_failing,
        verification_commands=verification,
        test_commands=tests,
        notes=notes,
        timeline=timeline,
        derivation=derive(session),
    )


def _build_timeline(session: Session) -> List[TimelineEntry]:
    entries: List[TimelineEntry] = []
    for event in _events(session):
        if not isinstance(event, Event):
            continue
        if event.type == EventType.NOTE.value:
            plain = normalize_text(_event_data(event).get("text", "")).strip()
            text = inline_text(plain)
            if text:
                entries.append(
                    TimelineEntry(event.timestamp, "note", text, plain)
                )
        elif event.type == EventType.COMMAND.value:
            data = CommandData.from_dict(event.data)
            label = status_label(data.classification.status)
            exit_repr = "n/a" if data.exit_code is None else str(data.exit_code)
            duration = f" [{_fmt_duration(data.duration_seconds)}]"
            plain = (
                f"{normalize_free_text(data.command)} -> "
                f"{normalize_free_text(data.classification.status)} "
                f"(exit {exit_repr}){duration}"
            )
            entries.append(
                TimelineEntry(
                    effective_event_timestamp(event),
                    "command",
                    f"{code_span(data.command)} -> {label} (exit {exit_repr}){duration}",
                    plain,
                    status=data.classification.status,
                    working_directory=report_working_directory(session, data.invocation_cwd),
                )
            )
        elif event.type == EventType.WARNING.value:
            plain = normalize_text(_event_data(event).get("message", "")).strip()
            text = inline_text(plain)
            if text:
                entries.append(
                    TimelineEntry(event.timestamp, "warning", text, plain)
                )
        elif event.type == EventType.SNAPSHOT.value:
            phase = _event_data(event).get("phase")
            if phase == "start":
                entries.append(
                    TimelineEntry(
                        event.timestamp, "snapshot", "Session started.", "Session started."
                    )
                )
            elif phase == "end":
                entries.append(
                    TimelineEntry(
                        event.timestamp, "snapshot", "Session ended.", "Session ended."
                    )
                )
    entries.sort(key=lambda e: _safe_seconds(e.timestamp))
    return entries


def _safe_seconds(iso_timestamp: str) -> float:
    try:
        return parse_iso8601(iso_timestamp).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0.0


def _fmt_duration(seconds: float) -> str:
    """Compact per-command duration, e.g. ``0.25s`` or ``3s``."""
    try:
        value = float(seconds)
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    if not math.isfinite(value) or value < 0:
        return "unknown"
    rounded = round(value, 3)
    if value > 0 and rounded == 0:
        return "<0.001s"
    if rounded < 60:
        return f"{rounded:.3f}".rstrip("0").rstrip(".") + "s"
    return human_duration(value)


def report_project_root(session: Session, include_full_path: bool = False) -> str:
    """Return the project-root value permitted in a generated report."""
    root = normalize_text(session.project_root).strip()
    if include_full_path:
        return root or "(unknown)"
    if not root:
        return "(unknown)"
    return Path(root).name or "."


def report_working_directory(session: Session, invocation_cwd: Optional[str]) -> Optional[str]:
    """Share only a recorded directory relative to the project, without disk access.

    Legacy, malformed, or outside-project locations stay unknown. Never invent
    the root as a fallback or resolve a historical symlink against today's disk.
    """
    if not invocation_cwd:
        return None
    cwd, root = Path(invocation_cwd), Path(session.project_root)
    if not cwd.is_absolute() or not root.is_absolute() or ".." in cwd.parts:
        return None
    try:
        return normalize_text(cwd.relative_to(root).as_posix())
    except ValueError:
        return None


def _cwd_suffix(session: Session, invocation_cwd: Optional[str]) -> str:
    directory = report_working_directory(session, invocation_cwd)
    return f" (working directory: {code_span(directory)})" if directory is not None else ""


def report_warnings(session: Session) -> List[str]:
    """Return plain normalized warnings from the summary and warning events."""
    warnings: List[str] = []
    stored_warnings = (
        session.warnings if isinstance(session.warnings, list) else []
    )
    for warning in stored_warnings:
        text = normalize_text(warning).strip()
        if text and text not in warnings:
            warnings.append(text)
    for event in _events(session):
        if not isinstance(event, Event):
            continue
        if event.type != EventType.WARNING.value:
            continue
        text = normalize_text(_event_data(event).get("message", "")).strip()
        if text and text not in warnings:
            warnings.append(text)
    return warnings


class BaseReporter:
    """Base class providing shared, reusable markdown section builders."""

    mode = "base"

    def __init__(self, context: ReportContext) -> None:
        self.ctx = context
        self.session = context.session
        self.detail = "full"  # "full" (default) or "compact"; set by render_report
        self.include_full_path = False

    # Subclasses must implement render().
    def render(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    # Reusable sections -------------------------------------------------------
    def title_line(self) -> str:
        return f"# {inline_text(self.session.title)}"

    def metadata_lines(self) -> List[str]:
        s = self.session
        lines = ["## Session metadata", ""]
        lines.append(f"- **Session ID:** {code_span(s.session_id)}")
        lines.append(f"- **Status:** {inline_text(s.status)}")
        lines.append(
            f"- **Project root:** "
            f"{code_span(report_project_root(s, self.include_full_path))}"
        )
        lines.append(f"- **Started:** {_short_time(s.timestamps.start)}")
        lines.append(f"- **Ended:** {_short_time(s.timestamps.end)}")
        if s.git.is_repo:
            branch = s.git.branch or (
                "(detached HEAD)" if s.git.detached_head else "(unknown)"
            )
            lines.append(f"- **Git branch:** {code_span(branch)}")
            lines.append(
                f"- **Initial commit:** {code_span(_sha(s.git.initial_sha))}  "
                f"**Final commit:** {code_span(_sha(s.git.final_sha))}"
            )
        else:
            lines.append("- **Git:** not a Git repository")
        lines.append(
            f"- **Notes:** {s.summary.notes_count}  "
            f"**Commands:** {s.summary.commands_count}  "
            f"**Failed commands:** {s.summary.failed_commands_count}"
        )
        return lines

    def warnings_section(self) -> List[str]:
        warnings = report_warnings(self.session)
        capture = inline_text(self.session.summary.command_capture_status)
        redacted = self.ctx.derivation.redaction_applied
        if not warnings and capture == "full" and not redacted:
            return []
        lines = ["## Warnings and limitations", ""]
        if capture != "full":
            lines.append(
                f"- Command capture status: **{capture}** "
                "(some commands may not have been recorded)."
            )
        if redacted:
            lines.append(
                "- Secret-like values in captured output, commands, or notes "
                "were replaced with `[redacted]`. Redaction is best effort and "
                "conservative; it does not catch everything."
            )
        for warning in warnings:
            lines.append(f"- {inline_text(warning)}")
        return lines

    _STATUS_WORDS = {
        "M": "modified",
        "A": "added",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
    }

    def changed_files_section(self) -> List[str]:
        s = self.session
        # Only render inside a repo and only when files actually changed. With
        # no real content the section is omitted rather than padded.
        if not s.git.is_repo:
            return []
        file_changes = (
            s.summary.file_changes
            if isinstance(s.summary.file_changes, list)
            else []
        )
        files = (
            s.summary.modified_files
            if isinstance(s.summary.modified_files, list)
            else []
        )
        if not file_changes and not files:
            return []
        lines = ["## Modified files", ""]
        count = len(file_changes) if file_changes else len(files)
        lines.append(
            f"_{count} file(s) changed, "
            f"+{s.summary.lines_added} / -{s.summary.lines_deleted} lines._"
        )
        lines.append("")
        if file_changes:
            for fc in file_changes:
                status = normalize_free_text(fc.status)
                word = self._STATUS_WORDS.get(status, inline_text(status))
                lines.append(f"- {code_span(status)} {word}: {code_span(fc.path)}")
        else:
            for path in files:
                lines.append(f"- {code_span(path)}")
        return lines

    def verification_section(self) -> List[str]:
        lines = ["## Verification and tests", ""]
        if self.ctx.derivation.invalid_verification_commands:
            lines.append(
                "_At least one verification record has no valid timestamp; "
                "this evidence does not verify the whole issue._"
            )
            lines.append("")
        verification = self.ctx.verification_commands
        if not verification:
            if self.ctx.currently_failing:
                if self.ctx.derivation.proof_command:
                    lines.append(
                        "_The latest recorded test/verification run is still failing. "
                        "This evidence does not verify the whole issue._"
                    )
                else:
                    lines.append(
                        "_One or more recorded commands were still failing. "
                        "This evidence does not verify the whole issue._"
                    )
            elif self.ctx.test_commands:
                lines.append(
                    "_Test/verification commands were run but none passed. "
                    "This evidence does not verify the whole issue._"
                )
            else:
                lines.append(
                    "_No verification commands (test/build/lint/typecheck) "
                    "were run during this session._"
                )
            return lines
        for rc in verification:
            kind = "test" if rc.is_test else "check"
            tool = f" ({inline_text(rc.tool)})" if rc.tool else ""
            repeat = f" x{rc.verification_count}" if rc.verification_count > 1 else ""
            directory = _cwd_suffix(self.session, rc.invocation_cwd)
            lines.append(f"- [passed] {kind}{tool}: {code_span(rc.command)}{repeat}{directory}")
        return lines

    def relevant_commands_section(
        self, heading: str = "## Relevant commands"
    ) -> List[str]:
        lines = [heading, ""]
        commands = self.ctx.report_commands
        if not commands:
            lines.append("_No notable commands were recorded._")
            return lines
        for rc in commands:
            repeat = f" x{rc.count}" if rc.count > 1 else ""
            exit_repr = "n/a" if rc.exit_code is None else str(rc.exit_code)
            lines.append(
                f"- {code_span(rc.command)}{repeat} -> {status_label(rc.status)} "
                f"(exit {exit_repr}){_cwd_suffix(self.session, rc.invocation_cwd)}"
            )
        return lines

    # Derived sections --------------------------------------------------------
    def one_liner_section(self) -> List[str]:
        one_liner = self.ctx.derivation.one_liner
        if not one_liner:
            return []
        return ["## Summary", "", one_liner]

    def evidence_section(self) -> List[str]:
        derivation = self.ctx.derivation
        return [
            "## Evidence status",
            "",
            f"- **State:** {inline_text(derivation.proof_state)}",
            f"- **Basis:** {inline_text(derivation.proof_basis)}",
            "- **Scope:** This verifies the recorded check, not the whole issue."
            if derivation.proof_state == "verified"
            else "- **Scope:** This describes recorded checks, not the whole issue.",
            "- **Root cause:** Not inferred by DebugBrief.",
        ]

    def reproduce_verify_section(self) -> List[str]:
        d = self.ctx.derivation
        if not d.reproduce_command and not d.verify_command:
            return []
        lines = ["## Reproduce and verify", ""]
        if d.reproduce_command:
            lines.append(
                f"- Reproduce (failed): {code_span(d.reproduce_command)}"
                + _cwd_suffix(self.session, d.reproduce_invocation_cwd)
            )
        if d.verify_command:
            label = (
                "Verify (passed)"
                if d.proof_state == "verified"
                else "Verify (earlier pass; overall unverified)"
            )
            lines.append(
                f"- {label}: {code_span(d.verify_command)}"
                + _cwd_suffix(self.session, d.verify_invocation_cwd)
            )
        return lines

    def red_to_green_section(self) -> List[str]:
        rtg = self.ctx.derivation.red_to_green
        if rtg is None:
            return []
        lines = ["## Red to green", ""]
        window = human_duration(rtg.window_seconds)
        lines.append(
            f"A check failed at `{_clock(rtg.failed_at)}` and {code_span(rtg.command)} "
            f"passed at `{_clock(rtg.passed_at)}` (window {window})."
        )
        if rtg.changed_files:
            lines.append("")
            lines.append(
                "Between the failing and passing checks, these files changed "
                "(correlation, not proven cause):"
            )
            for path in rtg.changed_files:
                lines.append(f"- {code_span(path)}")
        elif rtg.commit_boundary:
            lines.append("")
            lines.append(
                "A commit boundary was recorded between checks; file-level changes "
                "were not captured in working-tree snapshots."
            )
        else:
            lines.append("")
            lines.append(
                "No tracked file changes were recorded across this window."
            )
        return lines

    def timeline_section(
        self, heading: str = "## Timeline", condensed: bool = False
    ) -> List[str]:
        entries = self.ctx.timeline
        if condensed:
            entries = [e for e in entries if e.kind in ("note", "command", "warning")]
        if not entries:
            return []
        lines = [heading, ""]
        for entry in entries:
            directory = (
                f" (working directory: {code_span(entry.working_directory)})"
                if entry.working_directory is not None else ""
            )
            lines.append(f"- `{_clock(entry.timestamp)}` ({entry.kind}) {entry.text}{directory}")
        return lines

    def observed_error_section(self) -> List[str]:
        error = self.ctx.derivation.observed_error
        if not error:
            return []
        return [
            "## Observed error",
            "",
            "Excerpt from recorded command output:",
            "",
            fenced_code(error),
        ]

    def ruled_out_section(self) -> List[str]:
        ruled = self.ctx.derivation.ruled_out
        if not ruled:
            return []
        lines = ["## Failed attempts", ""]
        for rec in ruled:
            exit_repr = "n/a" if rec.exit_code is None else str(rec.exit_code)
            lines.append(
                f"- {code_span(rec.command)} -> {status_label(rec.status)} (exit {exit_repr})"
                + _cwd_suffix(self.session, rec.invocation_cwd)
            )
        return lines

    def footer(self) -> List[str]:
        return [
            "---",
            "",
            "_Generated by DebugBrief. This report is built from explicitly "
            "recorded notes, executed commands, and Git state when available. "
            "DebugBrief does not use AI and does not infer root causes, intent, "
            "or results._",
        ]


def _sha(value: Optional[str]) -> str:
    text = normalize_free_text(value)
    if not text:
        return "n/a"
    return text[:12]


def join_sections(*blocks: List[str]) -> str:
    """Join section line-lists into a single markdown document."""
    parts: List[str] = []
    for block in blocks:
        if not block:
            continue
        parts.append("\n".join(block))
    return "\n\n".join(parts).rstrip() + "\n"
