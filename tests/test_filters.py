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
    # A wrapper's own options are handled: a known boolean flag (--yes, -q,
    # --no-sync) consumes nothing, and any other long option consumes its value,
    # so the real command is found either way.
    assert filters.classify_command("uv run --with pytest pytest", 0).tool == "pytest"
    assert filters.classify_command("uv run --project pkgs/api pytest", 0).tool == "pytest"
    assert filters.classify_command("uv run --no-sync pytest", exit_code=0).tool == "pytest"
    assert filters.classify_command("poetry run -q pytest", exit_code=0).tool == "pytest"
    assert filters.classify_command("npx --yes jest", exit_code=0).tool == "jest"
    # An option's value is never mistaken for the command (no false positive).
    assert filters.classify_command("uv run --with pytest python app.py", 0).tool is None
    # An unlisted value option consumes its value too, so its value is not read as
    # the command. --env-file is not in the boolean list: its value is consumed,
    # the real command surfaces, and a non-test never poses as a passed test.
    assert filters.classify_command("uv run --env-file .env pytest", 0).tool == "pytest"
    assert filters.classify_command("uv run --env-file pytest python app.py", 0).tool is None


def test_every_recognized_test_pattern_classifies_by_exit_code():
    # Cross-language coverage: every recognized test runner is a test, named with
    # its tool, verified on exit 0 and a failed check on nonzero.
    for pattern, tool in filters._TEST_PATTERNS:
        cmd = " ".join(pattern)
        passed = filters.classify_command(cmd, exit_code=0)
        assert passed.is_test and passed.tool == tool and passed.is_verification, cmd
        failed = filters.classify_command(cmd, exit_code=1)
        assert failed.is_test and failed.tool == tool, cmd
        assert failed.is_verification is False, cmd


def test_every_recognized_build_pattern_classifies_by_exit_code():
    # Build/lint/typecheck runners verify on exit 0 and fail on nonzero too.
    for pattern, tool, _category in filters._BUILD_PATTERNS:
        cmd = " ".join(pattern)
        passed = filters.classify_command(cmd, exit_code=0)
        assert passed.tool == tool and passed.is_test is False and passed.is_verification, cmd
        failed = filters.classify_command(cmd, exit_code=1)
        assert failed.tool == tool and failed.is_verification is False, cmd


def test_unknown_check_is_classified_only_through_verify():
    # An unrecognized command is captured but not a verification unless declared.
    assert filters.classify_command("./scripts/it.sh", exit_code=0).tool is None
    declared = filters.classify_command("./scripts/it.sh", exit_code=0, force_verification=True)
    assert declared.tool == "custom" and declared.is_verification is True
    failed = filters.classify_command("./scripts/it.sh", exit_code=1, force_verification=True)
    assert failed.tool == "custom" and failed.is_verification is False


def _warn(cmd, pipefail, passed, force_verification=False):
    return filters.shell_command_warning(
        cmd, True, pipefail, passed=passed, force_verification=force_verification
    )


def test_simple_shell_check_classified_normally():
    # One stage: the exit code is the check's own, so pass/fail is honest.
    c = filters.classify_command("pytest -q", exit_code=0, use_shell=True, pipefail=True)
    assert c.tool == "pytest" and c.is_test is True and c.is_verification is True
    cf = filters.classify_command("pytest -q", exit_code=1, use_shell=True, pipefail=True)
    assert cf.tool == "pytest" and cf.is_test is True and cf.is_verification is False
    assert _warn("pytest -q", True, passed=True) is None


def test_compound_command_is_never_attributed_to_an_internal_tool():
    # Even a clean, reliable pipeline is recorded as a generic command, not as the
    # tool inside it: DebugBrief does not parse shell grammar to decide which stage
    # the exit code belongs to. The user runs the check on its own, or uses
    # --verify, to get a recorded verification.
    for cmd in ("pytest -q | tee out.txt", "cd packages/api && pytest | tee out",
                "pytest && ruff check ."):
        c = filters.classify_command(cmd, exit_code=0, use_shell=True, pipefail=True)
        assert c.tool is None and c.is_test is False and c.is_verification is False, cmd
        assert _warn(cmd, True, passed=True) is not None, cmd
    # Without --shell the "|" is a literal argument, a single command, so the
    # normal first-token classification still applies.
    assert filters.classify_command("pytest -q | tee out.txt", 0).tool == "pytest"


def test_compound_failure_is_not_attributed_to_a_tool():
    # The command failed but which stage failed is unknown: pytest may never have
    # run (cd failed) or may have passed (&& false, | false). Never blame pytest.
    for cmd in ("cd missing && pytest", "pytest && false", "pytest | false"):
        c = filters.classify_command(cmd, exit_code=1, use_shell=True, pipefail=True)
        assert c.tool is None and c.is_test is False and c.is_verification is False, cmd
        w = _warn(cmd, True, passed=False)
        assert w is not None and "cannot determine which stage" in w, cmd


def test_masking_and_unreliable_compounds_warn_and_do_not_verify():
    # Masking operators and unreliable pipelines never produce a verification; a
    # compound that contains a check is recorded generically with an explanation.
    cases = [
        ("pytest || true", True),                     # || masks a failure
        ("pytest; echo done", True),                  # ; discards pytest's exit
        ("pytest & echo bg", True),                   # & backgrounds pytest
        ("pytest | tee out", False),                  # pipe without pipefail
        ("set +o pipefail; pytest | tee out", True),  # pipefail disabled in command
    ]
    for cmd, pipefail in cases:
        c = filters.classify_command(cmd, exit_code=0, use_shell=True, pipefail=pipefail)
        assert c.is_verification is False and c.tool is None, cmd
        assert _warn(cmd, pipefail, passed=True) is not None, cmd


def test_exotic_shell_constructs_are_never_attributed_to_a_tool():
    # Substitutions, backticks, process substitution, subshells, braces, and
    # heredocs must never let a tool name be mistaken for the command that ran, so
    # none of these is classified as pytest. DebugBrief does not parse shell.
    constructs = [
        "echo $(pytest)",
        "echo `pytest`",
        "cat <(pytest)",
        "(pytest)",
        "{ pytest; }",
        "pytest <<EOF\nbody\nEOF",
    ]
    for cmd in constructs:
        for ec in (0, 1):
            c = filters.classify_command(cmd, exit_code=ec, use_shell=True, pipefail=True)
            assert c.tool != "pytest" and c.is_verification is False, (cmd, ec)


def test_verify_declares_reliable_compound_but_never_an_unreliable_pipe():
    # --verify makes a whole reliable compound a single custom check...
    cmd = "cd pkg && ./scripts/it.sh"
    c = filters.classify_command(
        cmd, exit_code=0, use_shell=True, pipefail=True, force_verification=True
    )
    assert c.tool == "custom" and c.is_verification is True
    assert _warn(cmd, True, passed=True, force_verification=True) is None
    # ...but never trusts a pipeline without pipefail, even with --verify.
    cu = filters.classify_command(
        "pytest | tee out", exit_code=0, use_shell=True, pipefail=False, force_verification=True
    )
    assert cu.is_verification is False and cu.tool is None


def test_shell_comments_are_ignored_when_scanning_for_operators():
    # Operators inside a trailing # comment are not real: the command is just the
    # part before the comment, so a recognized check still classifies.
    for cmd in ("pytest # compare a && b", "pytest # output | tee", "pytest  #note"):
        c = filters.classify_command(cmd, exit_code=0, use_shell=True, pipefail=True)
        assert c.tool == "pytest" and c.is_verification is True, cmd
        assert _warn(cmd, True, passed=True) is None, cmd
    # A '#' inside quotes is data, not a comment.
    assert filters._is_shell_pipeline("pytest -k 'a # b'") is False
    # A genuine compound with a trailing comment is still compound.
    assert filters._is_compound_shell("cd x && pytest # done") is True


def test_shell_negation_is_never_a_verification():
    # `! cmd` inverts the exit status, so exit 0 means the command failed; --verify
    # must not record a pass. Covers simple, pipeline, and setup-prefixed forms.
    for cmd in ("! pytest", "! pytest | tee out", "cd x && ! pytest"):
        c = filters.classify_command(
            cmd, exit_code=0, use_shell=True, pipefail=True, force_verification=True
        )
        assert c.is_verification is False and c.tool is None, cmd
        assert _warn(cmd, True, passed=True, force_verification=True) is not None, cmd
    # A '!' inside an argument or quotes is not negation.
    assert filters._has_shell_negation("pytest -k '!slow'") is False


def test_pipe_both_operator_is_a_reliable_pipeline():
    # `|&` pipes stdout and stderr; it is a pipeline operator, not a background &.
    # Under pipefail with --verify the declared check is therefore counted.
    cmd = "pytest |& tee out"
    c = filters.classify_command(
        cmd, exit_code=0, use_shell=True, pipefail=True, force_verification=True
    )
    assert c.tool == "custom" and c.is_verification is True
    assert _warn(cmd, True, passed=True, force_verification=True) is None


def test_verify_on_an_unreliable_compound_warns_that_it_was_ignored():
    # When --verify cannot be honored (unreliable exit code), say so, even when no
    # built-in tool is recognized, so the user is not left thinking it counted.
    for cmd, pipefail in (("make all || true", True), ("pytest | tee out", False)):
        w = _warn(cmd, pipefail, passed=True, force_verification=True)
        assert w is not None and "declared with --verify" in w, cmd


def test_quoted_and_escaped_pipes_are_not_pipelines():
    # A "|" inside quotes or escaped with a backslash is data, not an operator, so
    # the command is a single stage and classifies normally.
    for cmd in ("pytest -k 'a | b'", "pytest -k a\\|b"):
        assert filters._is_shell_pipeline(cmd) is False, cmd
        c = filters.classify_command(cmd, exit_code=0, use_shell=True, pipefail=False)
        assert c.tool == "pytest" and c.is_verification is True, cmd
        assert _warn(cmd, False, passed=True) is None, cmd
    # "||" is logical-or (a masking operator), not a pipe.
    assert filters._is_shell_pipeline("pytest || echo done") is False


def test_trailing_background_operator_is_compound_not_a_pass():
    # `pytest &` backgrounds the job and bash returns 0 immediately, so it must
    # not be recorded as a passed pytest even though it is the only segment.
    for cmd in ("pytest &", "pytest -q &"):
        c = filters.classify_command(cmd, exit_code=0, use_shell=True, pipefail=True)
        assert c.tool is None and c.is_verification is False, cmd
        assert _warn(cmd, True, passed=True) is not None, cmd


def test_descriptor_redirections_are_simple_commands():
    # The `&` in a redirection is part of one command, not a stage separator, so a
    # recognized check with a redirection still classifies normally.
    for cmd in ("pytest 2>&1", "pytest &>out.log", "pytest <&0", "pytest >out 2>&1"):
        c = filters.classify_command(cmd, exit_code=0, use_shell=True, pipefail=True)
        assert c.tool == "pytest" and c.is_verification is True, cmd
        assert _warn(cmd, True, passed=True) is None, cmd
    # A redirection inside a reliable compound does not make it unreliable.
    c = filters.classify_command(
        "cd pkg && pytest 2>&1", exit_code=0, use_shell=True, pipefail=True,
        force_verification=True,
    )
    assert c.tool == "custom" and c.is_verification is True


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
