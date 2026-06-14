"""Tests for command classification, noise filtering, and deduplication."""

from __future__ import annotations

from datetime import timedelta

from debugbrief import filters
from debugbrief.models import (
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_PASSED,
    COMMAND_STATUS_TIMED_OUT,
    CommandClassification,
    CommandData,
    Event,
)
from debugbrief.utils import to_iso8601, utc_now


def _make_command_event(command, status, ts, exit_code=0, is_test=False, is_verification=False, tool=None):
    data = CommandData(
        command=command,
        started_at=ts,
        ended_at=ts,
        duration_seconds=0.1,
        exit_code=exit_code,
        classification=CommandClassification(
            is_test=is_test,
            is_verification=is_verification,
            tool=tool,
            status=status,
        ),
    )
    return Event.command(data, ts)


# Classification -----------------------------------------------------------
def test_classify_pytest_pass_is_verification():
    cls = filters.classify_command("python -m pytest tests/", exit_code=0)
    assert cls.is_test is True
    assert cls.tool == "pytest"
    assert cls.status == COMMAND_STATUS_PASSED
    assert cls.is_verification is True


def test_classify_pytest_fail_is_not_verification():
    cls = filters.classify_command("pytest", exit_code=1)
    assert cls.is_test is True
    assert cls.status == COMMAND_STATUS_FAILED
    assert cls.is_verification is False


def test_classify_various_test_tools():
    cases = {
        "npm test": "npm",
        "npm run test": "npm",
        "pnpm test": "pnpm",
        "yarn test": "yarn",
        "jest --watch=false": "jest",
        "go test ./...": "go",
        "cargo test": "cargo",
        "rspec spec/": "rspec",
        "bundle exec rspec": "rspec",
        "mvn test": "maven",
        "gradle test": "gradle",
    }
    for command, tool in cases.items():
        cls = filters.classify_command(command, exit_code=0)
        assert cls.is_test is True, command
        assert cls.tool == tool, command


def test_classify_build_lint_typecheck():
    cases = {
        "npm run build": ("npm", True),
        "pnpm build": ("pnpm", True),
        "yarn build": ("yarn", True),
        "npm run lint": ("npm", True),
        "yarn lint": ("yarn", True),
        "npm run typecheck": ("npm", True),
        "mypy src": ("mypy", True),
        "ruff check .": ("ruff", True),
        "black --check .": ("black", True),
        "tsc --noEmit": ("tsc", True),
    }
    for command, (tool, verify_on_pass) in cases.items():
        cls = filters.classify_command(command, exit_code=0)
        assert cls.is_test is False, command
        assert cls.tool == tool, command
        assert cls.is_verification is verify_on_pass, command
        # Same command failing must NOT be verification.
        fail_cls = filters.classify_command(command, exit_code=1)
        assert fail_cls.is_verification is False, command


def test_classify_anchors_to_executable_not_arguments():
    # A tool name that only appears as an argument is not the tool.
    assert filters.classify_command("echo pytest", exit_code=0).tool is None
    assert filters.classify_command("python tool.py --label pytest", 0).tool is None
    # The executable is recognized by its basename, even via a path.
    assert filters.classify_command(".venv/bin/pytest -q", exit_code=0).tool == "pytest"
    assert filters.classify_command("/usr/local/bin/jest", exit_code=0).tool == "jest"
    # Common wrappers are unwrapped to the real tool underneath.
    assert filters.classify_command("python -m pytest", exit_code=0).tool == "pytest"
    assert filters.classify_command("uv run pytest", exit_code=0).tool == "pytest"
    assert filters.classify_command("poetry run pytest tests/", 0).tool == "pytest"
    assert filters.classify_command("npx jest", exit_code=0).tool == "jest"
    # A leading environment assignment is skipped.
    assert filters.classify_command("CI=1 pytest", exit_code=0).tool == "pytest"
    # A wrapper's own options are handled: a value-taking option (--with,
    # --project) is consumed with its value, and a boolean flag (--yes, -q,
    # --no-sync) consumes nothing, so the real command is found either way.
    assert filters.classify_command("uv run --with pytest pytest", 0).tool == "pytest"
    assert filters.classify_command("uv run --project pkgs/api pytest", 0).tool == "pytest"
    assert filters.classify_command("uv run --no-sync pytest", exit_code=0).tool == "pytest"
    assert filters.classify_command("poetry run -q pytest", exit_code=0).tool == "pytest"
    assert filters.classify_command("npx --yes jest", exit_code=0).tool == "jest"
    # An option's value is never mistaken for the command (no false positive).
    assert filters.classify_command("uv run --with pytest python app.py", 0).tool is None


def test_shell_pipeline_honesty():
    cmd = "pytest -q | tee out.txt"
    # With pipefail (bash) the exit status is reliable, so the pipeline is
    # classified normally: exit 0 means the whole pipeline passed.
    cp = filters.classify_command(cmd, exit_code=0, use_shell=True, pipefail=True)
    assert cp.tool == "pytest" and cp.is_verification is True
    # A failing stage under pipefail is correctly a failure, not a pass.
    cf = filters.classify_command(cmd, exit_code=1, use_shell=True, pipefail=True)
    assert cf.is_verification is False
    assert filters.shell_pipeline_suppressed_check(cmd, True, pipefail=True) is False
    # Without pipefail the exit status is only the last stage's, so a recognized
    # check in a pipeline is NOT treated as a verification and the runner warns.
    cu = filters.classify_command(cmd, exit_code=0, use_shell=True, pipefail=False)
    assert cu.is_verification is False and cu.tool is None and cu.is_test is False
    assert filters.shell_pipeline_suppressed_check(cmd, True, pipefail=False) is True
    # Without --shell the "|" is a literal argument, not a pipeline.
    assert filters.classify_command(cmd, 0).tool == "pytest"
    # "||" is logical-or, and a quoted "|" is not a pipe.
    assert filters.shell_pipeline_suppressed_check("pytest || echo done", True, False) is False
    assert filters.shell_pipeline_suppressed_check("pytest -k 'a | b'", True, False) is False


def test_setup_prefixed_pipeline_is_still_warned():
    # The check is not the first token, but a pipeline without pipefail still
    # suppresses it -- so the warning must fire rather than drop it silently.
    for cmd in ("cd packages/api && pytest | tee out", "set -o pipefail; pytest | tee out"):
        assert filters.shell_pipeline_suppressed_check(cmd, True, pipefail=False) is True
    # A pipeline with no recognized check anywhere does not warn.
    assert filters.shell_pipeline_suppressed_check("cd pkg && echo hi | tee out", True, False) is False


def test_classify_unknown_command():
    cls = filters.classify_command("echo hello", exit_code=0)
    assert cls.is_test is False
    assert cls.is_verification is False
    assert cls.tool is None
    assert cls.status == COMMAND_STATUS_PASSED


def test_classify_timeout_status():
    cls = filters.classify_command("pytest", exit_code=None, timed_out=True)
    assert cls.status == COMMAND_STATUS_TIMED_OUT
    assert cls.is_verification is False


# Noise --------------------------------------------------------------------
def test_is_noise_command():
    for cmd in ["ls", "ll", "pwd", "cd ..", "clear", "history", "cat file", "git status", "  ", ""]:
        assert filters.is_noise_command(cmd) is True, cmd
    for cmd in ["pytest", "git commit -m x", "python app.py"]:
        assert filters.is_noise_command(cmd) is False, cmd


def test_build_report_drops_noise_but_keeps_failed_noise():
    now = utc_now()
    t0 = to_iso8601(now)
    t1 = to_iso8601(now + timedelta(seconds=60))
    events = [
        _make_command_event("ls", COMMAND_STATUS_PASSED, t0, exit_code=0),
        _make_command_event("cat missing", COMMAND_STATUS_FAILED, t1, exit_code=1),
    ]
    report = filters.build_report_commands(events)
    commands = [rc.command for rc in report]
    assert "ls" not in commands  # passing noise dropped
    assert "cat missing" in commands  # failing noise retained


# Deduplication ------------------------------------------------------------
def test_dedup_within_window():
    now = utc_now()
    t0 = to_iso8601(now)
    t1 = to_iso8601(now + timedelta(seconds=10))
    events = [
        _make_command_event("pytest", COMMAND_STATUS_FAILED, t0, exit_code=1, is_test=True, tool="pytest"),
        _make_command_event("pytest", COMMAND_STATUS_PASSED, t1, exit_code=0, is_test=True, is_verification=True, tool="pytest"),
    ]
    report = filters.build_report_commands(events, dedup_window_seconds=30)
    assert len(report) == 1
    rc = report[0]
    assert rc.count == 2
    assert rc.last_timestamp == t1
    assert rc.status == COMMAND_STATUS_PASSED  # most recent outcome kept
    assert rc.exit_code == 0


def test_no_dedup_outside_window():
    now = utc_now()
    t0 = to_iso8601(now)
    t1 = to_iso8601(now + timedelta(seconds=45))
    events = [
        _make_command_event("pytest", COMMAND_STATUS_PASSED, t0),
        _make_command_event("pytest", COMMAND_STATUS_PASSED, t1),
    ]
    report = filters.build_report_commands(events, dedup_window_seconds=30)
    assert len(report) == 2
