"""Handoff-mode report: hand a partially solved or tricky issue to someone else."""

from __future__ import annotations

from typing import List

from ..derive import next_step_notes
from .base import BaseReporter, join_sections


class HandoffReporter(BaseReporter):
    mode = "handoff"

    def render(self) -> str:
        return join_sections(
            [self.title_line()],
            self.one_liner_section(),
            self.metadata_lines(),
            self.warnings_section(),
            self._current_status_section(),
            self._hypotheses_section(),
            self.timeline_section("## Timeline", condensed=False),
            self.relevant_commands_section("## Commands attempted"),
            self.ruled_out_section(),
            self._repo_state_section(),
            self._next_steps_section(),
            self.footer(),
        )

    def _current_status_section(self) -> List[str]:
        lines = ["## Current status", ""]
        verified = len(self.ctx.verification_commands) > 0
        failed = len(self.ctx.failed_commands)

        if verified and failed == 0:
            summary = (
                "At least one verification command passed and no recorded "
                "commands are currently failing."
            )
        elif failed:
            summary = (
                f"{failed} command(s) were failing when the session ended; "
                "work appears incomplete."
            )
        else:
            summary = (
                "No verification command passed; the state of the fix is "
                "unconfirmed."
            )
        lines.append(summary)
        return lines

    def _hypotheses_section(self) -> List[str]:
        lines = ["## Working hypotheses / findings", ""]
        if not self.ctx.notes:
            lines.append(
                "_No hypotheses or findings were recorded as notes during this "
                "session._"
            )
            return lines
        for _timestamp, text in self.ctx.notes:
            lines.append(f"- {text}")
        return lines

    def _repo_state_section(self) -> List[str]:
        s = self.session
        if not s.git.is_repo:
            return []
        lines = ["## Current repo state", ""]
        branch = s.git.branch or (
            "(detached HEAD)" if s.git.detached_head else "(unknown)"
        )
        lines.append(f"- Branch: {branch}")
        lines.append(f"- HEAD at session end: `{_sha(s.git.final_sha)}`")
        lines.append(
            f"- Uncommitted changes: {len(s.summary.modified_files)} file(s), "
            f"+{s.summary.lines_added} / -{s.summary.lines_deleted} lines."
        )
        return lines

    def _next_steps_section(self) -> List[str]:
        # Next steps are drawn only from recorded notes, never inferred.
        steps = next_step_notes(self.session)
        if not steps:
            return []
        lines = ["## Suggested next steps", ""]
        lines.append("_Based only on the notes you recorded:_")
        for note in steps:
            lines.append(f"- {note}")
        return lines


def _sha(value):
    if not value:
        return "n/a"
    return value[:12]
