from __future__ import annotations

import base64
import hashlib
import re
from html.parser import HTMLParser

import pytest

from debugbrief import cli
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
from debugbrief.paths import ProjectPaths
from debugbrief.reporters import render_report_html
from debugbrief.reports_index import first_title, latest_report
from debugbrief.session_manager import SessionManager
from debugbrief.sessions_index import report_modes_for


def _session() -> Session:
    session = Session(
        title="Trust <tag>",
        project_root="/private/workspace/MyRepo",
        session_id="11111111-2222-4333-8444-555555555555",
        status=SessionStatus.COMPLETED.value,
        git=GitState(
            is_repo=True,
            repo_root="/private/workspace/MyRepo",
            initial_sha="a",
            final_sha="b",
            branch="main",
        ),
        timestamps=Timestamps(
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T00:00:20Z",
        ),
    )
    session.events.append(Event.note("check <tag>", "2026-01-01T00:00:01Z"))
    session.events.append(
        Event.command(
            CommandData(
                command="pytest -q",
                started_at="2026-01-01T00:00:02Z",
                ended_at="2026-01-01T00:00:03Z",
                duration_seconds=1.0,
                exit_code=0,
                classification=CommandClassification(
                    status=COMMAND_STATUS_PASSED,
                    is_test=True,
                    is_verification=True,
                    tool="pytest",
                ),
            ),
            "2026-01-01T00:00:03Z",
        )
    )
    session.summary = Summary(
        modified_files=["src/app.py"],
        file_changes=[FileChange("M", "src/app.py")],
        commands_count=1,
        notes_count=1,
    )
    return session


def test_html_report_is_self_contained_and_escaped():
    document = render_report_html(_session(), "pr")
    assert document.startswith("<!doctype html>")
    assert "evidence" in document
    assert "verified" in document
    assert "Root cause is not inferred" in document
    assert "/private/workspace/MyRepo" not in document
    assert "MyRepo" in document
    assert "<tag>" not in document
    assert "&lt;tag&gt;" in document
    class Assets(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name in {"href", "src"}:
                    assert value.startswith("data:"), (tag, name, value)

    Assets().feed(document)
    assert "http://" not in document and "https://" not in document
    style = re.search(r"<style>(.*?)</style>", document, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert f"style-src 'sha256-{digest}'" in document
    assert "default-src 'none'" in document


@pytest.mark.parametrize("seconds, expected", [
    (0, "0s"),
    (0.0000001, "&lt;0.001s"),
    (0.418000000004, "0.418s"),
    (46.74799990653992, "46.748s"),
    (59.9999, "1m 0s"),
    (77.217, "1m 17s"),
])
def test_html_command_and_timeline_durations_are_readable(seconds, expected):
    session = _session()
    session.command_events()[0].data["duration_seconds"] = seconds
    document = render_report_html(session, "pr")
    assert f"exit 0 · {expected}</p>" in document
    assert f"[{expected}]" in document


def test_html_report_wraps_narrow_metadata_and_timeline_content():
    document = render_report_html(_session(), "pr")
    assert ".meta div{display:flex;gap:6px;min-width:0}" in document
    assert ".meta dd{margin:0;min-width:0;overflow-wrap:anywhere;word-break:break-word}" in document
    assert ".timeline li{position:relative;min-width:0;" in document
    assert "h1,h2,.lede,.proof p,.section li,.section p,.report-top span,.footer" in document


def test_html_report_supports_all_modes_and_full_path_opt_in():
    session = _session()
    for mode in ("pr", "handoff", "incident"):
        document = render_report_html(session, mode)
        assert f"debugbrief / {mode}" in document
    assert "Current status" in render_report_html(session, "handoff")
    assert "Time window" in render_report_html(session, "incident")
    assert "Resolution / current state" in render_report_html(session, "incident")
    assert "<dt>Project</dt>" in render_report_html(session, "pr")
    full = render_report_html(session, "pr", include_full_path=True)
    assert "/private/workspace/MyRepo" in full


def test_html_failed_attempts_only_lists_failures():
    session = _session()
    failed = CommandData(
        command="pytest -q",
        started_at="2026-01-01T00:00:02Z",
        ended_at="2026-01-01T00:00:03Z",
        duration_seconds=1.0,
        exit_code=1,
        classification=CommandClassification(
            status=COMMAND_STATUS_FAILED,
            is_test=True,
            tool="pytest",
        ),
    )
    session.events.insert(1, Event.command(failed, "2026-01-01T00:00:03Z"))
    document = render_report_html(session, "pr")
    section = document.split("<h2>Failed attempts</h2>", 1)[1].split("</section>", 1)[0]
    assert "failed" in section
    assert "passed" not in section


def test_html_does_not_double_escape_paths_in_plain_summaries():
    session = _session()
    session.summary.modified_files = ["<tag>.py"]
    session.summary.file_changes = [FileChange("M", "<tag>.py")]
    document = render_report_html(session, "pr")
    assert "&amp;lt;tag&amp;gt;.py" not in document
    assert "&lt;tag&gt;.py" in document


def test_end_writes_html_and_last_finds_it(tmp_path, monkeypatch, capsys):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    manager = SessionManager(paths)
    manager.start("html report")
    manager.add_note("a finding")
    assert cli.main(["end", "--format", "html"]) == 0
    session_file = next(paths.sessions_dir.glob("*.json"))
    session_id = session_file.stem
    report = paths.report_html_file(session_id, "pr")
    assert report.exists()
    assert report.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert latest_report(paths.reports_dir) == report
    assert first_title(report) == "html report"
    assert report_modes_for(paths, session_id) == ["pr"]
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    assert "reports:      pr" in capsys.readouterr().out
    assert cli.main(["show", session_id]) == 0
    assert ".html" in capsys.readouterr().out


def test_html_shows_escaped_output_and_marks_failure_in_the_timeline():
    session = _session()
    command = session.events[-1].data
    command.update(
        exit_code=1, stdout_preview="first\n<script>unsafe</script>\nlast", stdout_truncated=True
    )
    command["classification"]["status"] = "failed"
    document = render_report_html(session, "pr")
    assert "<h2>Command output</h2>" in document
    assert "<script>unsafe</script>" not in document
    assert "first\n&lt;script&gt;unsafe&lt;/script&gt;\nlast" in document
    assert "truncated preview" in document
    assert 'class="command-event command-failed"' in document
    assert '<details class="record">' in document


def test_html_never_uses_a_passing_timeline_marker_for_a_failure():
    session = _session()
    session.events[-1].data["exit_code"] = 2
    document = render_report_html(session, "pr")
    assert 'class="command-event command-passed"' not in document
    assert 'class="command-event command-failed"' in document


def test_same_timestamp_outcomes_keep_distinct_timeline_statuses():
    session = _session()
    failure = CommandData.from_dict(session.events[-1].data)
    failure.exit_code = 1
    failure.classification.status = "failed"
    failure.classification.is_verification = False
    session.events.insert(1, Event.command(failure, session.events[-1].timestamp))
    document = render_report_html(session, "pr")
    assert 'class="command-event command-failed"' in document
    assert 'class="command-event command-passed"' in document


@pytest.mark.parametrize("extra_pass, expected_count", [(False, 1), (True, 2)])
def test_verification_repeats_count_only_actual_passes(extra_pass, expected_count):
    from debugbrief.reporters import build_context, render_report

    session = _session()
    passed = session.command_events()[0]
    failure = CommandData.from_dict(passed.data)
    failure.exit_code = 1
    failure.classification.status = "failed"
    failure.classification.is_verification = False
    session.events.insert(1, Event.command(failure, passed.timestamp))
    if extra_pass:
        session.events.append(Event.command(CommandData.from_dict(passed.data), passed.timestamp))
    context = build_context(session)
    assert context.report_commands[0].count == expected_count + 1
    assert context.verification_commands[0].verification_count == expected_count
    html = render_report_html(session, "pr")
    verification = html.split("<h2>Verification</h2>", 1)[1].split("</section>", 1)[0]
    md = render_report(session, "pr")
    md_verification = md.split("## Verification and tests", 1)[1].split("\n## ", 1)[0]
    if expected_count == 1:
        assert "×" not in verification
        assert " x2" not in md_verification
    else:
        assert "×2" in verification
        assert " x2" in md_verification
