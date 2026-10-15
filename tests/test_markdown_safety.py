"""Markdown is generated safely for arbitrary commands, output, and filenames."""

from __future__ import annotations

from debugbrief.markdown import code_span, fenced_code, inline_path, plain_markdown
from debugbrief.models import (
    COMMAND_STATUS_FAILED,
    CommandClassification,
    CommandData,
    Event,
    FileChange,
    GitState,
    Session,
    SessionStatus,
    Summary,
    Timestamps,
)
from debugbrief.reporters import render_report


def test_plain_markdown_removes_delimiters_but_keeps_literal_backticks():
    assert plain_markdown("Run " + code_span("echo `date`") + " now") == "Run echo `date` now"


def test_plain_markdown_unescapes_html_once_and_paths_preserve_spaces():
    assert plain_markdown("changed &lt;tag&gt;.py") == "changed <tag>.py"
    assert inline_path("dir/  spaced name.py") == "dir/  spaced name.py"


# code_span -------------------------------------------------------------------
def test_code_span_plain_is_unchanged():
    assert code_span("pytest -q") == "`pytest -q`"


def test_code_span_with_backticks_uses_longer_delimiter():
    out = code_span("echo `date`")
    assert out == "`` echo `date` ``"  # longer fence + padding, content intact
    assert "`date`" in out


def test_code_span_run_of_backticks():
    out = code_span("a ``` b")
    # delimiter must be longer than the 3-backtick run inside
    assert out.startswith("````") and out.endswith("````")
    assert "```" in out


def test_code_span_leading_trailing_backtick_is_padded():
    assert code_span("`x`") == "`` `x` ``"


def test_code_span_leading_space_preserved():
    # CommonMark strips one leading/trailing space; padding compensates.
    assert code_span(" leading") == "`  leading `"


def test_code_span_newlines_flattened():
    assert "\n" not in code_span("line1\nline2")


# fenced_code -----------------------------------------------------------------
def test_fenced_code_plain():
    assert fenced_code("hello\nworld") == "```\nhello\nworld\n```"


def test_fenced_code_with_triple_backtick_line():
    text = "before\n```\ninside\n```\nafter"
    out = fenced_code(text)
    # opening/closing fence must be longer than any run inside (4 backticks)
    assert out.startswith("````\n") and out.endswith("\n````")
    lines = out.splitlines()
    assert lines[0] == "````" and lines[-1] == "````"
    assert "```" in out  # the content's own backticks are preserved


def test_fenced_code_with_quadruple_backtick_line():
    out = fenced_code("x\n````\ny")
    assert out.startswith("`````")


# End to end: report stays structurally valid -------------------------------
def _session_with(command, stdout, path):
    session = Session(
        title="md safety",
        project_root="/repo",
        session_id="11111111-2222-4333-8444-555555555555",
        status=SessionStatus.COMPLETED.value,
        git=GitState(is_repo=True, repo_root="/repo", initial_sha="a", final_sha="b",
                     branch="main"),
        timestamps=Timestamps(start="2026-01-02T03:04:05.000Z",
                              end="2026-01-02T03:04:10.000Z"),
    )
    data = CommandData(
        command=command, started_at="2026-01-02T03:04:05.000Z",
        ended_at="2026-01-02T03:04:06.000Z", duration_seconds=0.2, exit_code=1,
        stdout_preview=stdout,
        classification=CommandClassification(status=COMMAND_STATUS_FAILED,
                                             is_test=True, tool="pytest"),
    )
    session.events.append(Event.command(data, "2026-01-02T03:04:05.000Z"))
    session.summary = Summary(
        modified_files=[path], file_changes=[FileChange("M", path)],
        commands_count=1, failed_commands_count=1, command_capture_status="full",
    )
    return session


def test_report_with_backtick_command_and_output_stays_balanced():
    error_line = "AssertionError: expected ``` fence but got `code`"
    session = _session_with(
        command="echo `whoami` && grep ``` file",
        stdout=f"collected 1 item\n{error_line}\n1 failed",
        path="weird`name`.py",
    )
    for mode in ("pr", "handoff", "incident"):
        report = render_report(session, mode)
        # The command (with its backticks) appears in every mode's timeline.
        assert "whoami" in report

    # PR mode lists changed filenames; a backtick in one is rendered safely.
    pr = render_report(session, "pr")
    assert "weird`name`.py" in pr

    # The observed-error line itself contains a ``` run; the block must be opened
    # with a longer fence so that run cannot close it early.
    incident = render_report(session, "incident")
    assert error_line in incident
    lines = incident.splitlines()
    fence_open = lines[lines.index("Excerpt from recorded command output:") + 2]
    assert set(fence_open) == {"`"} and len(fence_open) >= 4


def test_modified_filenames_and_legacy_command_error_data_are_safe():
    session = _session_with(
        command="echo legacy\n## command heading\x1b[2J",
        stdout="ok\n``` injected fence\x00",
        path="safe\n## filename <script>alert(1)</script>`name.py",
    )
    session.summary.file_changes[0].status = "M\n## status"
    session.events[0].data["stderr_preview"] = (
        "trace\n```\n\x1b[31merror\x00\n</script>\nAssertionError: ``` fence"
    )
    for mode in ("pr", "handoff", "incident"):
        report = render_report(session, mode)
        assert "\n## filename" not in report
        assert "\n## command" not in report
        assert "\x1b" not in report
        assert "\x00" not in report
        if "## Modified files" in report:
            assert "safe ## filename" in report
    incident = render_report(session, "incident")
    fence_open = incident.splitlines()[
        incident.splitlines().index("Excerpt from recorded command output:") + 2
    ]
    assert len(fence_open) >= 4


def test_code_helpers_accept_non_text_and_strip_controls():
    assert "\x1b" not in code_span({"command": "echo\x1b[2J"})
    assert "\x1b" not in fenced_code({"error": "line\x00\n```"})
