"""PR-mode report: a pull-request-ready summary of a debugging session.

Every section is derived from recorded evidence and is omitted when it has no
real content, so the report never pads itself with templated filler.
"""

from __future__ import annotations

from typing import List

from .base import BaseReporter, join_sections


class PRReporter(BaseReporter):
    mode = "pr"

    def render(self) -> str:
        if self.detail == "compact":
            return self._render_compact()
        return join_sections(
            [self.title_line()],
            self.one_liner_section(),
            self.metadata_lines(),
            self.warnings_section(),
            self.reproduce_verify_section(),
            self.red_to_green_section(),
            self.changed_files_section(),
            self.timeline_section("## Timeline", condensed=True),
            self.verification_section(),
            self.ruled_out_section(),
            self.footer(),
        )

    def _render_compact(self) -> str:
        """A scannable summary with the heavier sections collapsed.

        The same information as the full report, but only the summary, changed
        files, and verification stay open; metadata, the timeline, and the rest
        move into a collapsible section so the brief is short by default.
        """
        heavy = join_sections(
            self.metadata_lines(),
            self.warnings_section(),
            self.reproduce_verify_section(),
            self.red_to_green_section(),
            self.timeline_section("## Timeline", condensed=True),
            self.ruled_out_section(),
        )
        details: List[str] = []
        if heavy.strip():
            details = [
                "<details>",
                "<summary>Full timeline and metadata</summary>",
                "",
                heavy,
                "",
                "</details>",
            ]
        return join_sections(
            [self.title_line()],
            self.one_liner_section(),
            self.changed_files_section(),
            self.verification_section(),
            details,
            self.footer(),
        )
