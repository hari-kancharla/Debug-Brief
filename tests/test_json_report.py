"""Tests for the JSON report output and its agreement with the markdown."""

from __future__ import annotations

from datetime import timedelta

import pytest

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
from debugbrief.reporters.base import report_working_directory
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
        git=GitState(
            is_repo=True, repo_root="/repo", initial_sha="aaa", final_sha="bbb", branch="main"
        ),
        timestamps=Timestamps(start=t0, end=t1),
    )
    s.events.append(Event.note("a hypothesis", t0))
    s.events.append(
        _cmd(
            "pytest",
            COMMAND_STATUS_FAILED,
            t0,
            1,
            ["src/x.py"],
            stderr="AssertionError: nope",
            is_test=True,
            tool="pytest",
        )
    )
    s.events.append(
        _cmd(
            "pytest",
            COMMAND_STATUS_PASSED,
            t1,
            0,
            ["src/x.py"],
            is_test=True,
            is_verification=True,
            tool="pytest",
        )
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


@pytest.mark.parametrize("cwd, expected", [
    ("/repo", "."),
    ("/repo/packages/cache", "packages/cache"),
    ("/private/other-project", None),
    ("/repo/../other-project", None),
    ("relative/path", None),
    (None, None),
])
def test_report_directories_are_relative_and_never_guessed(cwd, expected):
    assert report_working_directory(_session(), cwd) == expected


def test_all_report_formats_retain_where_the_check_actually_ran():
    from debugbrief.reporters import render_report, render_report_html

    session = _session()
    for event in session.command_events():
        event.data["invocation_cwd"] = "/repo/packages/cache"
    original = session.to_dict()
    for mode in ("pr", "handoff", "incident"):
        payload = render_report_json(session, mode)
        assert payload["reproduce_working_directory"] == "packages/cache"
        assert payload["verify_working_directory"] == "packages/cache"
        assert payload["red_to_green"]["working_directory"] == "packages/cache"
        assert all(c["working_directory"] == "packages/cache" for c in payload["commands"])
        assert all(c["working_directory"] == "packages/cache" for c in payload["verification"])
        assert all(c["working_directory"] == "packages/cache" for c in payload["ruled_out"])
        assert all(e["working_directory"] == "packages/cache" for e in payload["timeline"] if e["kind"] == "command")
        assert "working directory: `packages/cache`" in render_report(session, mode)
        assert "Working directory: packages/cache" in render_report_html(session, mode)
        assert "/repo" not in str(payload)
    assert session.to_dict() == original


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


def test_structured_command_history_keeps_both_attempts_and_bounded_output():
    session = _session()
    session.events[1].data["stdout_preview"] = "failure details <script>"
    session.events[1].data["stdout_truncated"] = True
    session.events[1].data["invocation_cwd"] = "/private/workspace/repo"
    payload = render_report_json(session, "pr")
    assert payload["schema_version"] == 1
    assert len(payload["commands"]) == 2
    failed, passed = payload["commands"]
    assert failed["status"] == "failed" and failed["exit_code"] == 1
    assert failed["stdout_preview"] == "failure details <script>"
    assert failed["stdout_truncated"] is True
    assert passed["status"] == "passed" and passed["is_verification"] is True
    assert "invocation_cwd" not in failed
    assert "fingerprints" not in failed


def test_structured_commands_do_not_promote_help_or_nonzero_exit_to_verification():
    session = _session()
    session.events[-1].data.update(command="pytest --help")
    payload = render_report_json(session, "pr")
    assert payload["commands"][-1]["is_verification"] is False


def test_current_failures_remain_after_an_earlier_pass():
    session = _session()
    session.events[-1].data["exit_code"] = 1
    payload = render_report_json(session, "pr")
    assert payload["evidence"]["state"] == "unverified"
    assert payload["currently_failing"][0]["exit_code"] == 1
    assert all(not item["is_verification"] for item in payload["commands"])
