"""Tests for the JSON report output and its agreement with the markdown."""

from __future__ import annotations

from datetime import timedelta

from debugbrief.models import (
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_PASSED,
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
from debugbrief.reporters import build_context, render_report_json
from debugbrief.utils import to_iso8601, utc_now


def _cmd(command, status, ts, exit_code, changed_files=None, stderr="", **cls):
    data = CommandData(
        command=command,
        started_at=ts,
        ended_at=ts,
        duration_seconds=0.2,
        exit_code=exit_code,
        stderr_preview=stderr,
        classification=CommandClassification(status=status, **cls),
        git_changed_files=list(changed_files or []),
    )
    return Event.command(data, ts)


def _session():
    now = utc_now()
    t0 = to_iso8601(now)
    t1 = to_iso8601(now + timedelta(seconds=20))
    s = Session(
        title="Fix it",
        project_root="/repo",
        status=SessionStatus.COMPLETED.value,
        git=GitState(is_repo=True, repo_root="/repo", initial_sha="aaa", final_sha="bbb", branch="main"),
        timestamps=Timestamps(start=t0, end=t1),
    )
    s.events.append(Event.note("a hypothesis", t0))
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_FAILED, t0, 1, ["src/x.py"], stderr="AssertionError: nope", is_test=True, tool="pytest")
    )
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_PASSED, t1, 0, ["src/x.py"], is_test=True, is_verification=True, tool="pytest")
    )
    s.summary = Summary(
        modified_files=["src/x.py"],
        file_changes=[FileChange("M", "src/x.py")],
        lines_added=5,
        lines_deleted=1,
        notes_count=1,
        commands_count=2,
        failed_commands_count=1,
    )
    return s


def test_json_has_expected_keys():
    payload = render_report_json(_session(), "pr")
    for key in [
        "mode",
        "session_id",
        "title",
        "git",
        "counts",
        "line_stats",
        "one_liner",
        "evidence",
        "reproduce_command",
        "verify_command",
        "red_to_green",
        "observed_error",
        "ruled_out",
        "timeline",
        "changed_files",
        "verification",
        "notes",
        "redaction_applied",
        "command_capture_status",
    ]:
        assert key in payload, key
    assert payload["mode"] == "pr"
    assert payload["evidence"]["state"] == "verified"
    assert payload["evidence"]["root_cause"] == "not_inferred"
    assert "recorded check" in payload["evidence"]["scope"]
    assert payload["command_capture_status"] == "full"


def test_json_matches_markdown_derived_content():
    session = _session()
    payload = render_report_json(session, "pr")
    ctx = build_context(session)

    assert payload["one_liner"] and "`" not in payload["one_liner"]
    assert "pytest" in payload["one_liner"]
    assert payload["one_liner"] in render_to_md(session).replace("`", "")

    # Reproduce/verify and observed error agree with the derivation.
    assert payload["reproduce_command"] == ctx.derivation.reproduce_command
    assert payload["verify_command"] == ctx.derivation.verify_command
    assert payload["observed_error"] == ctx.derivation.observed_error

    # Red to green is present and carries the correlated files.
    assert payload["red_to_green"] is not None
    assert payload["red_to_green"]["changed_files"] == ["src/x.py"]

    # Counts and changed files line up with the summary.
    assert payload["counts"]["commands"] == 2
    assert payload["line_stats"] == {"added": 5, "deleted": 1}
    assert payload["changed_files"] == [{"status": "M", "path": "src/x.py"}]


def test_json_preserves_structured_command_and_path_whitespace():
    session = _session()
    session.events[-1].data["command"] = "echo  a   b"
    session.summary.modified_files = ["dir/  spaced name.py"]
    session.summary.file_changes = [FileChange("M", "dir/  spaced name.py")]
    payload = render_report_json(session, "pr")
    assert payload["verify_command"] == "echo  a   b"
    assert payload["changed_files"][0]["path"] == "dir/  spaced name.py"


def test_json_summary_preserves_literal_backticks_in_commands():
    session = _session()
    session.events[-1].data["command"] = "echo `date`"
    payload = render_report_json(session, "pr")
    assert "echo `date`" in payload["one_liner"]


def render_to_md(session):
    from debugbrief.reporters import render_report

    return render_report(session, "pr")
