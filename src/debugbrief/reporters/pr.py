"""PR-mode report: a pull-request-ready summary of a debugging session.

Every section is derived from recorded evidence and is omitted when it has no
real content, so the report never pads itself with templated filler.
"""

from __future__ import annotations

from .base import BaseReporter, join_sections


class PRReporter(BaseReporter):
    mode = "pr"

    def render(self) -> str:
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
