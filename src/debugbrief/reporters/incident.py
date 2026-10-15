"""Incident-mode report: a chronological, timeline-oriented engineering note."""

from __future__ import annotations

from typing import List

from ..markdown import code_span, inline_text
from ..utils import parse_iso8601
from .base import BaseReporter, join_sections


class IncidentReporter(BaseReporter):
    mode = "incident"

    def render(self) -> str:
        return join_sections(
            [self.title_line()],
            self.one_liner_section(),
            self.evidence_section(),
            self.metadata_lines(),
            self.warnings_section(),
            self._time_window_section(),
            self.timeline_section("## Chronological event timeline", condensed=False),
            self.observed_error_section(),
            self._resolution_section(),
            self.verification_section(),
            self._followup_section(),
            self.footer(),
        )

    def _time_window_section(self) -> List[str]:
        s = self.session
        lines = ["## Time window", ""]
        start = s.timestamps.start
        end = s.timestamps.end
        lines.append(f"- **Start:** {inline_text(start) if start else 'unknown'}")
        lines.append(f"- **End:** {inline_text(end) if end else 'unknown'}")
        duration = _duration(start, end)
        if duration is not None:
            lines.append(f"- **Duration:** {duration}")
        return lines

    def _resolution_section(self) -> List[str]:
        s = self.session
        lines = ["## Resolution / current state", ""]
        verified = self.ctx.derivation.proof_state == "verified"
        failed = len(self.ctx.currently_failing)
        if verified and failed == 0:
            lines.append(
                "A verification command passed and no commands are failing. "
                "This supports a passing check; it does not independently establish "
                "incident resolution."
            )
        elif failed:
            lines.append(
                f"{failed} command(s) were failing at the end of the session; "
                "the incident does not appear fully resolved."
            )
        else:
            lines.append(
                "No verification command passed; the resolution state is "
                "unconfirmed by the recorded evidence."
            )
        modified_files = (
            s.summary.modified_files
            if isinstance(s.summary.modified_files, list)
            else []
        )
        if s.git.is_repo and modified_files:
            lines.append("")
            lines.append(
                f"Session changes: {len(modified_files)} file(s) changed, "
                f"+{s.summary.lines_added} / -{s.summary.lines_deleted} lines."
            )
        return lines

    def _followup_section(self) -> List[str]:
        lines = ["## Follow-up items", ""]
        items: List[str] = []
        for rc in self.ctx.currently_failing:
            items.append(f"Resolve the failing command {code_span(rc.command)}.")
        if not self.ctx.verification_commands:
            items.append("Add a passing verification command to confirm resolution.")
        if not items:
            return []
        for item in items:
            lines.append(f"- {item}")
        return lines


def _duration(start, end):
    if not start or not end:
        return None
    try:
        delta = parse_iso8601(end) - parse_iso8601(start)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None
    total = int(delta.total_seconds())
    if total < 0:
        return None
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
