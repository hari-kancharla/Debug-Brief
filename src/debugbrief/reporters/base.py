"""Shared building blocks for markdown report generation.

A :class:`ReportContext` is computed once from a finalized session and contains
only deterministic, evidence-backed data. Reporters consume it to render their
mode-specific markdown. No reporter invents root causes, intent, or results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..derive import Derivation, derive
from ..filters import ReportCommand, build_report_commands
from ..markdown import code_span, fenced_code
from ..models import (
    COMMAND_STATUS_BROKEN_PIPE,
    COMMAND_STATUS_ERROR,
    COMMAND_STATUS_INTERRUPTED,
    COMMAND_STATUS_PASSED,
    COMMAND_STATUS_TIMED_OUT,
    CommandData,
    EventType,
    Session,
)
from ..utils import human_duration, parse_iso8601


@dataclass
class TimelineEntry:
    timestamp: str
    kind: str
    text: str


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


def _short_time(iso_timestamp: Optional[str]) -> str:
    if not iso_timestamp:
        return "unknown time"
    try:
        moment = parse_iso8601(iso_timestamp)
    except (ValueError, TypeError):
        return iso_timestamp
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _clock(iso_timestamp: Optional[str]) -> str:
    if not iso_timestamp:
        return "--:--:--"
    try:
        moment = parse_iso8601(iso_timestamp)
    except (ValueError, TypeError):
        return iso_timestamp
    return moment.strftime("%H:%M:%S")


def status_label(status: str) -> str:
    return {
        COMMAND_STATUS_PASSED: "passed",
        "failed": "failed",
        COMMAND_STATUS_TIMED_OUT: "timed out",
        COMMAND_STATUS_ERROR: "did not run",
        COMMAND_STATUS_INTERRUPTED: "interrupted",
        COMMAND_STATUS_BROKEN_PIPE: "broken pipe",
    }.get(status, status)


def build_context(session: Session) -> ReportContext:
    command_events = session.command_events()
    report_commands = build_report_commands(command_events)

    failed = [rc for rc in report_commands if rc.failed]
    verification = [rc for rc in report_commands if rc.is_verification]
    tests = [rc for rc in report_commands if rc.is_test]

    # "Currently failing" is judged by each command's latest outcome, not by any
    # historical failure: a check that failed and was later fixed is not failing.
    latest_by_check: Dict[Tuple[str, Optional[str]], ReportCommand] = {}
    for rc in report_commands:
        key = (rc.command, rc.invocation_cwd)
        prev = latest_by_check.get(key)
        if prev is None or rc.last_timestamp > prev.last_timestamp:
            latest_by_check[key] = rc
    currently_failing = [rc for rc in latest_by_check.values() if rc.failed]

    notes: List[Tuple[str, str]] = []
    for event in session.note_events():
        text = (event.data or {}).get("text", "").strip()
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
    for event in session.events:
        if event.type == EventType.NOTE.value:
            text = (event.data or {}).get("text", "").strip()
            if text:
                entries.append(TimelineEntry(event.timestamp, "note", text))
        elif event.type == EventType.COMMAND.value:
            data = CommandData.from_dict(event.data)
            label = status_label(data.classification.status)
            exit_repr = "n/a" if data.exit_code is None else str(data.exit_code)
            duration = f" [{_fmt_duration(data.duration_seconds)}]"
            entries.append(
                TimelineEntry(
                    event.timestamp,
                    "command",
                    f"{code_span(data.command)} -> {label} (exit {exit_repr}){duration}",
                )
            )
        elif event.type == EventType.WARNING.value:
            message = (event.data or {}).get("message", "").strip()
            if message:
                entries.append(TimelineEntry(event.timestamp, "warning", message))
        elif event.type == EventType.SNAPSHOT.value:
            phase = (event.data or {}).get("phase")
            if phase == "start":
                entries.append(
                    TimelineEntry(event.timestamp, "snapshot", "Session started.")
                )
            elif phase == "end":
                entries.append(
                    TimelineEntry(event.timestamp, "snapshot", "Session ended.")
                )
    entries.sort(key=lambda e: _safe_seconds(e.timestamp))
    return entries


def _safe_seconds(iso_timestamp: str) -> float:
    try:
        return parse_iso8601(iso_timestamp).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _fmt_duration(seconds: float) -> str:
    """Compact per-command duration, e.g. ``0.25s`` or ``3s``."""
    if seconds < 10:
        return f"{seconds:g}s"
    return human_duration(seconds)


class BaseReporter:
    """Base class providing shared, reusable markdown section builders."""

    mode = "base"

    def __init__(self, context: ReportContext) -> None:
        self.ctx = context
        self.session = context.session

    # Subclasses must implement render().
    def render(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    # Reusable sections -------------------------------------------------------
    def title_line(self) -> str:
        return f"# {self.session.title}"

    def metadata_lines(self) -> List[str]:
        s = self.session
        lines = ["## Session metadata", ""]
        lines.append(f"- **Session ID:** {code_span(s.session_id)}")
        lines.append(f"- **Status:** {s.status}")
        lines.append(f"- **Project root:** {code_span(s.project_root)}")
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
        warnings = self.session.warnings
        capture = self.session.summary.command_capture_status
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
            lines.append(f"- {warning}")
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
        file_changes = s.summary.file_changes
        files = s.summary.modified_files
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
                word = self._STATUS_WORDS.get(fc.status, fc.status)
                lines.append(f"- `{fc.status}` {word}: {code_span(fc.path)}")
        else:
            for path in files:
                lines.append(f"- {code_span(path)}")
        return lines

    def verification_section(self) -> List[str]:
        lines = ["## Verification and tests", ""]
        verification = self.ctx.verification_commands
        if not verification:
            if self.ctx.test_commands:
                lines.append(
                    "_Test/verification commands were run but none passed. "
                    "This work is **not** verified._"
                )
            else:
                lines.append(
                    "_No verification commands (test/build/lint/typecheck) "
                    "were run during this session._"
                )
            return lines
        for rc in verification:
            kind = "test" if rc.is_test else "check"
            tool = f" ({rc.tool})" if rc.tool else ""
            repeat = f" x{rc.count}" if rc.count > 1 else ""
            lines.append(f"- [passed] {kind}{tool}: {code_span(rc.command)}{repeat}")
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
                f"(exit {exit_repr})"
            )
        return lines

    # Derived sections --------------------------------------------------------
    def one_liner_section(self) -> List[str]:
        one_liner = self.ctx.derivation.one_liner
        if not one_liner:
            return []
        return ["## Summary", "", one_liner]

    def reproduce_verify_section(self) -> List[str]:
        d = self.ctx.derivation
        if not d.reproduce_command and not d.verify_command:
            return []
        lines = ["## Reproduce and verify", ""]
        if d.reproduce_command:
            lines.append(f"- Reproduce (failed): {code_span(d.reproduce_command)}")
        if d.verify_command:
            lines.append(f"- Verify (passed): {code_span(d.verify_command)}")
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
            lines.append(f"- `{_clock(entry.timestamp)}` ({entry.kind}) {entry.text}")
        return lines

    def observed_error_section(self) -> List[str]:
        error = self.ctx.derivation.observed_error
        if not error:
            return []
        return [
            "## Observed error",
            "",
            "Quoted verbatim from real command output:",
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
            )
        return lines

    def footer(self) -> List[str]:
        return [
            "---",
            "",
            "_Generated by DebugBrief. This report is built from explicitly "
            "recorded notes, executed commands, and Git state. DebugBrief does "
            "not use AI and does not infer root causes, intent, or results._",
        ]


def _sha(value: Optional[str]) -> str:
    if not value:
        return "n/a"
    return value[:12]


def join_sections(*blocks: List[str]) -> str:
    """Join section line-lists into a single markdown document."""
    parts: List[str] = []
    for block in blocks:
        if not block:
            continue
        parts.append("\n".join(block))
    return "\n\n".join(parts).rstrip() + "\n"
