"""Handoff-mode report: hand a partially solved or tricky issue to someone else."""

from __future__ import annotations

from typing import List

from .base import BaseReporter, _clock, join_sections, status_label


class HandoffReporter(BaseReporter):
    mode = "handoff"

    def render(self) -> str:
        return join_sections(
            [self.title_line()],
            self.metadata_lines(),
            self.warnings_section(),
            self._current_status_section(),
            self._hypotheses_section(),
            self._timeline_section(),
            self._commands_attempted_section(),
            self.changed_files_section(),
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

    def _timeline_section(self) -> List[str]:
        lines = ["## Timeline of meaningful steps", ""]
        meaningful = [e for e in self.ctx.timeline if e.kind in ("note", "command", "warning")]
        if not meaningful:
            lines.append("_No meaningful steps were recorded._")
            return lines
        for entry in meaningful:
            lines.append(f"- `{_clock(entry.timestamp)}` ({entry.kind}) {entry.text}")
        return lines

    def _commands_attempted_section(self) -> List[str]:
        lines = ["## Commands attempted", ""]
        if not self.ctx.report_commands:
            lines.append("_No notable commands were recorded._")
            return lines
        for rc in self.ctx.report_commands:
            repeat = f" x{rc.count}" if rc.count > 1 else ""
            exit_repr = "n/a" if rc.exit_code is None else str(rc.exit_code)
            lines.append(
                f"- `{rc.command}`{repeat} -> {status_label(rc.status)} "
                f"(exit {exit_repr})"
            )
        return lines

    def _repo_state_section(self) -> List[str]:
        s = self.session
        lines = ["## Current repo state", ""]
        if not s.git.is_repo:
            lines.append("_Not a Git repository._")
            return lines
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
        lines = ["## Suggested next steps", ""]
        items: List[str] = []
        for rc in self.ctx.failed_commands:
            items.append(f"Investigate the failing command `{rc.command}`.")
        if not self.ctx.verification_commands:
            items.append(
                "Run and pass a verification command (tests/build/lint/typecheck) "
                "to confirm the fix."
            )
        if self.session.git.is_repo and self.session.summary.modified_files:
            items.append("Review the uncommitted changes listed above.")
        if not items:
            items.append(
                "No automatic next steps were derived. Use the notes and timeline "
                "above to continue."
            )
        for item in items:
            lines.append(f"- {item}")
        return lines


def _sha(value):
    if not value:
        return "n/a"
    return value[:12]
