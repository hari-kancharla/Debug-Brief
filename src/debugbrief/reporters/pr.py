"""PR-mode report: a pull-request-ready summary of a debugging session."""

from __future__ import annotations

from typing import List

from .base import BaseReporter, join_sections


class PRReporter(BaseReporter):
    mode = "pr"

    def render(self) -> str:
        return join_sections(
            [self.title_line()],
            self.metadata_lines(),
            self.warnings_section(),
            self._overview_section(),
            self._key_findings_section(),
            self._changes_section(),
            self.changed_files_section(),
            self.verification_section(),
            self.relevant_commands_section(),
            self._risks_section(),
            self.footer(),
        )

    def _overview_section(self) -> List[str]:
        s = self.session
        lines = ["## Overview", ""]
        files = len(s.summary.modified_files)
        commands = s.summary.commands_count
        failed = s.summary.failed_commands_count
        verified = len(self.ctx.verification_commands) > 0

        sentences = [
            f"This pull request summarizes the debugging session "
            f"\"{s.title}\"."
        ]
        if s.git.is_repo:
            if files:
                sentences.append(
                    f"{files} file(s) were changed "
                    f"(+{s.summary.lines_added} / -{s.summary.lines_deleted})."
                )
            else:
                sentences.append("No file changes were detected in the working tree.")
        sentences.append(
            f"{commands} command(s) were recorded"
            + (f", {failed} of which failed." if commands else ".")
        )
        sentences.append(
            "Verification: at least one verification command passed."
            if verified
            else "Verification: no verification command passed (see below)."
        )
        lines.append(" ".join(sentences))
        return lines

    def _key_findings_section(self) -> List[str]:
        lines = ["## Key findings", ""]
        if not self.ctx.notes:
            lines.append(
                "_No findings were recorded as notes. Add context with "
                '`debugbrief note "..."` during future sessions._'
            )
            return lines
        for _timestamp, text in self.ctx.notes:
            lines.append(f"- {text}")
        return lines

    def _changes_section(self) -> List[str]:
        s = self.session
        lines = ["## Changes implemented", ""]
        if not s.git.is_repo:
            lines.append(
                "_Not a Git repository; implementation changes were not tracked._"
            )
            return lines
        if not s.summary.modified_files:
            lines.append("_No file changes were detected for this session._")
            return lines
        lines.append(
            f"{len(s.summary.modified_files)} file(s) changed, "
            f"+{s.summary.lines_added} / -{s.summary.lines_deleted} lines. "
            "See the modified files below; rationale, if any, is captured under "
            "Key findings."
        )
        return lines

    def _risks_section(self) -> List[str]:
        lines = ["## Risks / follow-up", ""]
        items: List[str] = []

        if not self.ctx.verification_commands:
            items.append(
                "These changes are **not** backed by a passing verification "
                "command (test/build/lint/typecheck). Verify before merging."
            )
        if self.ctx.failed_commands:
            items.append(
                f"{len(self.ctx.failed_commands)} command(s) failed during the "
                "session; confirm they are resolved or expected."
            )
        if self.session.git.detached_head:
            items.append(
                "Work was done on a detached HEAD; ensure changes are on a branch."
            )
        if not items:
            items.append(
                "No outstanding risks were detected automatically. Review manually."
            )
        for item in items:
            lines.append(f"- {item}")
        return lines
