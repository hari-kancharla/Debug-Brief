from __future__ import annotations

import io
import json
import sys

import pytest

from debugbrief import cli, git_utils
from debugbrief.command_runner import RunResult
from debugbrief.derive import derive
from debugbrief.markdown import normalize_free_text
from debugbrief.models import (
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_PASSED,
    CommandClassification,
    CommandData,
    Event,
    GitState,
    Session,
    SessionStatus,
    Summary,
    Timestamps,
    effective_event_timestamp,
)
from debugbrief.paths import ProjectPaths
from debugbrief.redo_safety import confirm_redo, is_destructive_command
from debugbrief.reporters import build_context, render_report, render_report_json
from debugbrief.session_manager import SessionManager


def _command_event(command, status, started_at, ended_at, exit_code, **classification):
    data = CommandData(
        command=command,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=1.0,
        exit_code=exit_code,
        classification=CommandClassification(status=status, **classification),
    )
    return Event.command(data, started_at)


def _session(root="/private/workspace/MyRepo"):
    return Session(
        title="Trust test",
        project_root=root,
        status=SessionStatus.COMPLETED.value,
        git=GitState(
            is_repo=True,
            repo_root=root,
            initial_sha="a",
            final_sha="b",
            branch="main",
        ),
        timestamps=Timestamps(
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T00:00:20Z",
        ),
        summary=Summary(commands_count=0, command_capture_status="full"),
    )


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "unlink build/output.txt",
        "sudo shred secrets.txt",
        "git reset --hard HEAD",
        "git clean -fd",
        "git clean -f -e -n",
        "kubectl delete pods x --as alice --as-group --dry-run=client",
        "kubectl exec pod -- bash -c 'rm -rf /cache' --dry-run=client",
        "git push --force-with-lease",
        "git restore src/app.py",
        "git checkout -- src/app.py",
        "git switch --discard-changes main",
        "git switch -C feature/auth",
        "git switch --force main",
        "git switch -f main",
        "git switch -Cbranch",
        "git switch -fCbranch",
        "git branch -D old-feature",
        "docker system prune -af",
        "docker compose down --volumes",
        "docker-compose -f compose.yml down --volumes",
        "docker-compose --project-directory services down --volumes",
        "docker compose --log-level INFO down",
        "helm --kubeconfig /tmp/kubeconfig delete release",
        "kubectl --token secret delete pods --all",
        "docker run --device /dev/null alpine rm -rf /cache",
        "docker run --unknown-option value alpine rm -rf /cache",
        "env -S 'rm -rf build'",
        "env -S -i rm -rf build",
        "env -S -- rm -rf build",
        "env -Srm -rf build",
        "env -P /usr/bin rm -rf build",
        "kubectl delete pods --all",
        "kubectl exec pod -- rm -rf /tmp/cache",
        "terraform destroy",
        "npm publish",
        "make deploy",
        "echo ready && rm -rf build",
        "echo $(rm -rf build)",
        "echo `rm -rf build`",
        "eval 'rm -rf build'",
        "bash -lc 'rm -rf build'",
        "bash -euc 'echo ready'",
        "docker run --rm alpine bash -lc 'rm -rf /tmp/cache'",
        "docker compose run --rm web sh -c 'rm -rf /tmp/cache'",
        "kubectl exec pod -- bash -lc 'rm -rf /tmp/cache'",
        "kubectl exec -it pod -- sh -c 'rm -rf /tmp/cache'",
        "docker -H unix:///var/run/docker.sock exec c rm -rf /tmp/cache",
        "kubectl -n ns exec -it pod -- sh -c 'rm -rf /tmp/cache'",
        "rm -rf target --help",
        "rm --dry-run target",
        "make clean --help",
        "cargo clean --help",
        "aws s3 rm bucket --help",
        "python -c 'import shutil; shutil.rmtree(\"build\")' --help",
        "bash",
        "kubectl delete pods --dry-run=none",
        "python3.12 -c 'print(1)'",
        "php -r 'echo 1;'",
        "git rm generated.txt",
        "git rm --help",
    ],
)
def test_destructive_redo_detector_conservatively_matches(command):
    assert is_destructive_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "echo hello",
        "git status",
        "docker ps",
        "docker-compose -f compose.yml config",
        "rm --help",
        "docker run --help",
        "kubectl --help",
        "git clean -n",
        "git clean -e node_modules -n",
        "kubectl delete pods --as alice --dry-run=client",
        "env -S 'printf safe'",
        "kubectl delete pods --dry-run=client",
        "terraform plan",
    ],
)
def test_destructive_redo_detector_leaves_ordinary_commands_alone(command):
    assert not is_destructive_command(command)


def test_inline_interpreter_requires_confirmation_in_shell_context():
    assert is_destructive_command(
        "python -c 'import shutil; shutil.rmtree(\"build\")'", used_shell=True
    )
    assert is_destructive_command("python -c 'print(1)'", used_shell=False)
    assert is_destructive_command("node -e 'console.log(1)'", used_shell=False)


def test_redo_confirmation_is_fail_closed_and_yes_bypasses_it():
    assert not confirm_redo("rm -rf build", stdin=io.StringIO(""))
    assert confirm_redo("rm -rf build", assume_yes=True, stdin=io.StringIO(""))
    assert confirm_redo("pytest -q", stdin=io.StringIO(""))


def test_redo_dry_run_does_not_lease_or_execute(tmp_path, monkeypatch, capsys):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    manager = SessionManager(paths)
    session = manager.start("dry run")

    def no_lease(*args, **kwargs):
        raise AssertionError("dry run acquired a lease")

    def no_execute(*args, **kwargs):
        raise AssertionError("dry run executed a command")

    monkeypatch.setattr(SessionManager, "command_lease", no_lease)
    monkeypatch.setattr(cli, "run_command", no_execute)
    session.events.append(
        _command_event("echo dry-run", COMMAND_STATUS_PASSED, "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", 0)
    )
    manager.save_session(session)

    assert cli.main(["redo", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "echo dry-run" in captured.out
    assert "(redo --dry-run)" in captured.out
    assert not paths.active_command_file.exists()
    assert len(SessionManager(paths).load_active().command_events()) == 1


def test_redo_confirmation_does_not_execute_on_eof_and_yes_runs(
    tmp_path, monkeypatch, capsys
):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    manager = SessionManager(paths)
    session = manager.start("confirmation")
    session.events.append(
        _command_event("rm -rf build", COMMAND_STATUS_PASSED, "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", 0)
    )
    manager.save_session(session)
    calls = []

    def fake_run_command(**kwargs):
        calls.append(kwargs)
        return RunResult(
            command_data=CommandData(
                command=kwargs["command"],
                started_at="2026-01-01T00:00:02Z",
                ended_at="2026-01-01T00:00:03Z",
                duration_seconds=1.0,
                exit_code=0,
                classification=CommandClassification(status=COMMAND_STATUS_PASSED),
            ),
            timed_out=False,
            errored=False,
        )

    monkeypatch.setattr(cli, "run_command", fake_run_command)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["redo"]) == 1
    assert not calls
    assert not paths.active_command_file.exists()

    assert cli.main(["redo", "--yes"]) == 0
    assert len(calls) == 1
    assert len(SessionManager(paths).load_active().command_events()) == 2


def test_reports_hide_root_by_default_and_full_path_is_opt_in():
    root = "/private/workspace/MyRepo"
    session = _session(root)
    markdown = render_report(session, "pr")
    payload = render_report_json(session, "pr")
    assert "`MyRepo`" in markdown
    assert root not in markdown
    assert payload["project_root"] == "MyRepo"
    assert root not in json.dumps(payload)

    full_markdown = render_report(session, "pr", include_full_path=True)
    full_payload = render_report_json(session, "pr", include_full_path=True)
    assert root in full_markdown
    assert full_payload["project_root"] == root


def test_cli_reports_hide_root_by_default(tmp_path, monkeypatch):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    SessionManager(paths).start("safe path")
    assert cli.main(["end", "--format", "both"]) == 0
    report = next(paths.reports_dir.glob("*-pr.md")).read_text(encoding="utf-8")
    payload = json.loads(next(paths.reports_dir.glob("*-pr.json")).read_text(encoding="utf-8"))
    assert str(tmp_path) not in report
    assert payload["project_root"] == tmp_path.name


def test_preview_and_end_include_full_path_flag(tmp_path, monkeypatch, capsys):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    SessionManager(paths).start("path flag")

    assert cli.main(["preview", "--include-full-path"]) == 0
    assert str(tmp_path) in capsys.readouterr().out

    assert cli.main(["end", "--include-full-path", "--format", "both"]) == 0
    session = next(paths.sessions_dir.glob("*.json"))
    raw = json.loads(session.read_text(encoding="utf-8"))
    assert raw["status"] == "COMPLETED"
    report = next(paths.reports_dir.glob("*-pr.md")).read_text(encoding="utf-8")
    payload = json.loads(next(paths.reports_dir.glob("*-pr.json")).read_text(encoding="utf-8"))
    assert str(tmp_path) in report
    assert payload["project_root"] == str(tmp_path)


def test_include_full_path_config_cannot_enable_sensitive_output(tmp_path, monkeypatch, capsys):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    (tmp_path / ".debugbrief.toml").write_text(
        "include_full_path = true\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    SessionManager(paths).start("config path")
    assert cli.main(["preview"]) == 0
    output = capsys.readouterr().out
    assert str(tmp_path) not in output
    assert tmp_path.name in output
def test_chronology_uses_command_end_times():
    session = _session()
    session.events = [
        _command_event(
            "pytest",
            COMMAND_STATUS_PASSED,
            "2026-01-01T00:00:01Z",
            "2026-01-01T00:00:02Z",
            0,
            is_test=True,
            is_verification=True,
            tool="pytest",
        ),
        _command_event(
            "pytest",
            COMMAND_STATUS_FAILED,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:10Z",
            1,
            is_test=True,
            tool="pytest",
        ),
    ]
    context = build_context(session)
    command_entries = [entry for entry in context.timeline if entry.kind == "command"]
    assert command_entries[0].timestamp.endswith("00:00:02Z")
    assert command_entries[1].timestamp.endswith("00:00:10Z")
    assert context.derivation.red_to_green is None


def test_explicit_false_path_flag_overrides_config(tmp_path, monkeypatch, capsys):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    (tmp_path / ".debugbrief.toml").write_text(
        "include_full_path = true\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    SessionManager(paths).start("explicit false")
    assert cli.main(["preview", "--no-include-full-path"]) == 0
    assert str(tmp_path) not in capsys.readouterr().out


def test_old_command_without_ended_at_uses_event_timestamp():
    session = _session()
    data = CommandData(
        command="legacy",
        started_at="2026-01-01T00:00:01Z",
        ended_at="",
        duration_seconds=1.0,
        exit_code=0,
        classification=CommandClassification(status=COMMAND_STATUS_PASSED),
    )
    session.events = [Event.command(data, "2026-01-01T00:00:05Z")]
    context = build_context(session)
    assert context.timeline[0].timestamp == "2026-01-01T00:00:05Z"


def test_new_command_events_use_ended_at(tmp_path):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    manager = SessionManager(paths)
    manager.start("ended")
    result = RunResult(
        command_data=CommandData(
            command="echo done",
            started_at="2026-01-01T00:00:00Z",
            ended_at="2026-01-01T00:00:09Z",
            duration_seconds=9.0,
            exit_code=0,
            classification=CommandClassification(status=COMMAND_STATUS_PASSED),
        ),
        timed_out=False,
        errored=False,
    )
    manager.record_command(result)
    event = manager.load_active().command_events()[0]
    assert event.timestamp == event.data["ended_at"] == "2026-01-01T00:00:09Z"


def test_free_form_report_text_cannot_break_structure():
    session = _session()
    session.title = "Title\n## injected\x1b"
    session.warnings = ["warning\n<script>alert(1)</script>\x00"]
    session.events = [
        Event.note(
            "finding\n- injected <img src=x onerror=alert(1)>",
            "2026-01-01T00:00:01Z",
        ),
        Event.note(
            "TODO: follow up\n<script>next step</script>",
            "2026-01-01T00:00:02Z",
        ),
    ]
    session.summary.notes_count = 2
    report = render_report(session, "handoff")
    payload = render_report_json(session, "handoff")
    assert "\n## injected" not in report
    assert "<script>" not in report
    assert "<img" not in report
    assert "&lt;script&gt;" in report
    assert "\x1b" not in report
    assert "## Suggested next steps" in report
    assert payload["title"] == normalize_free_text(session.title)
    assert payload["warnings"]


def test_ambiguous_shell_redo_requires_confirmation_without_running(tmp_path, monkeypatch):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    manager = SessionManager(paths)
    session = manager.start("ambiguous")
    data = CommandData(
        command="echo $(rm -rf build)",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        duration_seconds=1.0,
        exit_code=0,
        used_shell=True,
        classification=CommandClassification(status=COMMAND_STATUS_PASSED),
    )
    session.events.append(Event.command(data, data.ended_at))
    manager.save_session(session)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(
        cli,
        "run_command",
        lambda *args, **kwargs: pytest.fail("ambiguous redo executed a command"),
    )
    assert cli.main(["redo"]) == 1
    assert len(SessionManager(paths).load_active().command_events()) == 1
    assert not paths.active_command_file.exists()


def test_keyboard_interrupt_during_confirmation_declines(monkeypatch):
    def interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert not confirm_redo("rm -rf build", stdin=None)


def test_dry_run_reports_provenance_refusal_without_side_effects(tmp_path, monkeypatch, capsys):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    manager = SessionManager(paths)
    session = manager.start("dry refusal")
    session.events.append(
        _command_event(
            "true",
            COMMAND_STATUS_PASSED,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
            0,
        )
    )
    manager.save_session(session)
    monkeypatch.setattr(
        git_utils, "tracked_state", lambda *args: git_utils.TrackedState.UNKNOWN
    )
    monkeypatch.setattr(
        SessionManager,
        "command_lease",
        lambda *args, **kwargs: pytest.fail("dry run acquired a lease"),
    )
    monkeypatch.setattr(
        cli,
        "run_command",
        lambda *args, **kwargs: pytest.fail("dry run executed a command"),
    )
    assert cli.main(["redo", "--dry-run"]) == 1
    captured = capsys.readouterr()
    assert "would be refused" in captured.err
    assert "could not confirm" in captured.err
    assert not paths.active_command_file.exists()


def test_invalid_ended_at_falls_back_and_selects_newest_command():
    old = _command_event(
        "old",
        COMMAND_STATUS_PASSED,
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:01Z",
        0,
    )
    data = CommandData(
        command="new",
        started_at="2026-01-01T00:00:02Z",
        ended_at="not-a-timestamp",
        duration_seconds=1.0,
        exit_code=0,
        classification=CommandClassification(status=COMMAND_STATUS_PASSED),
    )
    new = Event.command(data, "2026-01-01T00:00:03Z")
    assert effective_event_timestamp(new) == "2026-01-01T00:00:03Z"
    assert cli._latest_command_event([old, new]) is new
    older_by_time = _command_event(
        "older-by-time",
        COMMAND_STATUS_PASSED,
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:09Z",
        0,
    )
    assert cli._latest_command_event([older_by_time, new]) is new


def test_json_scalars_are_plain_but_markdown_is_escaped():
    session = _session()
    session.title = "Title *stars* <tag>"
    session.warnings = ["warning _under_ <tag>"]
    session.events = [Event.note("note *stars* <tag>", "2026-01-01T00:00:01Z")]
    payload = render_report_json(session, "handoff")
    assert payload["title"] == "Title *stars* <tag>"
    assert payload["warnings"] == ["warning _under_ <tag>"]
    assert payload["notes"] == ["note *stars* <tag>"]
    report = render_report(session, "handoff")
    assert "&lt;tag&gt;" in report
    assert "\\*stars\\*" in report


def test_multiline_notes_stay_multiline_in_storage_and_json(tmp_path):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    manager = SessionManager(paths)
    manager.start("multiline")
    note = "first line\n- injected <tag>\nlast line"
    manager.add_note(note)
    session = manager.load_active()
    assert session.note_events()[0].data["text"] == note
    payload = render_report_json(session, "handoff")
    assert payload["notes"] == [note]
    report = render_report(session, "handoff")
    assert "first line<br>" in report
    assert "\\- injected" in report
    assert "\n- injected" not in report


def test_redacted_warnings_mark_the_report_as_redacted():
    session = _session()
    session.add_warning("token=ghp_abcdefghij1234567890ABCDEFGHIJ", "2026-01-01T00:00:01Z")
    assert derive(session).redaction_applied is True


def test_persisted_display_fields_are_terminal_safe():
    session = _session()
    session.title = "Title\x1b]0;clipboard\x07"
    session.project_root = "/tmp/\x1b[2Jrepo"
    session.warnings = ["warning\x00"]
    rebuilt = Session.from_dict(session.to_dict())
    assert "\x1b" not in rebuilt.title
    assert "\x1b" not in rebuilt.project_root
    assert "\x00" not in rebuilt.warnings[0]


def test_malformed_leaf_state_loads_and_renders_without_traceback(tmp_path):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    paths.ensure_directories()
    sid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    body = {
        "session_id": sid,
        "title": ["bad"],
        "status": {"bad": True},
        "project_root": 12,
        "warnings": [None, "warning"],
        "git": {"is_repo": "false", "repo_root": [], "initial_dirty": []},
        "timestamps": {"start": {}, "end": 1},
        "events": [
            {
                "type": "command",
                "timestamp": "bad",
                "data": {
                    "command": None,
                    "started_at": {},
                    "ended_at": [],
                    "duration_seconds": "bad",
                    "exit_code": {},
                    "used_shell": "true",
                    "classification": "bad",
                    "git_changed_files": "bad",
                },
            }
        ],
        "summary": {
            "modified_files": "bad",
            "file_changes": "bad",
            "tests_run": {},
            "command_capture_status": [],
        },
    }
    (paths.sessions_dir / f"{sid}.json").write_text(json.dumps(body), encoding="utf-8")
    session = SessionManager(paths).load_session_file(sid)
    assert render_report(session, "incident")
    assert render_report_json(session, "incident")["title"] == ""


@pytest.mark.parametrize(
    "command",
    [
        "echo $(rm -rf build)",
        "eval 'rm -rf build'",
        "bash -lc 'rm -rf build'",
        "docker run --rm alpine bash -lc 'rm -rf /tmp/cache'",
        "kubectl exec pod -- bash -lc 'rm -rf /tmp/cache'",
    ],
)
def test_ambiguous_redo_cases_never_call_runner(command, tmp_path, monkeypatch):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    manager = SessionManager(paths)
    session = manager.start("nested")
    data = CommandData(
        command=command,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        duration_seconds=1.0,
        exit_code=0,
        used_shell=True,
        classification=CommandClassification(status=COMMAND_STATUS_PASSED),
    )
    session.events.append(Event.command(data, data.ended_at))
    manager.save_session(session)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(
        cli,
        "run_command",
        lambda *args, **kwargs: pytest.fail("redo executed an ambiguous command"),
    )
    assert cli.main(["redo"]) == 1
    assert len(SessionManager(paths).load_active().command_events()) == 1
