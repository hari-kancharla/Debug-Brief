"""Unit tests for the deterministic report derivations."""

from __future__ import annotations

from datetime import timedelta

from debugbrief.derive import derive, next_step_notes
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
from debugbrief.reporters import build_context
from debugbrief.utils import to_iso8601, utc_now

NOW = utc_now()


def _ts(offset_seconds):
    return to_iso8601(NOW + timedelta(seconds=offset_seconds))


def _cmd(
    command, status, ts, exit_code, changed_files=None, stderr="", stdout="",
    invocation_cwd=None, **cls,
):
    data = CommandData(
        command=command,
        started_at=ts,
        ended_at=ts,
        duration_seconds=0.2,
        exit_code=exit_code,
        stderr_preview=stderr,
        stdout_preview=stdout,
        classification=CommandClassification(status=status, **cls),
        git_changed_files=list(changed_files or []),
        invocation_cwd=invocation_cwd,
    )
    return Event.command(data, ts)


def _session(is_repo=True, modified=None, start=0, end=10):
    session = Session(
        title="t",
        project_root="/repo",
        status=SessionStatus.COMPLETED.value,
        git=GitState(is_repo=is_repo, repo_root="/repo" if is_repo else None),
        timestamps=Timestamps(start=_ts(start), end=_ts(end)),
    )
    if modified:
        session.summary = Summary(
            modified_files=list(modified),
            file_changes=[FileChange("M", p) for p in modified],
        )
    return session


# one-liner ----------------------------------------------------------------
def test_empty_tool_does_not_create_a_verification_claim():
    s = _session()
    data = CommandData(
        command="custom-check",
        started_at=_ts(0),
        ended_at=_ts(1),
        duration_seconds=1,
        exit_code=0,
        classification=CommandClassification(
            status=COMMAND_STATUS_PASSED,
            is_verification=True,
            tool="",
        ),
    )
    s.events.append(Event.command(data, _ts(1)))
    d = derive(s)
    assert d.proof_state == "unverified"
    assert d.verify_command is None


def test_one_liner_all_present():
    s = _session(modified=["src/auth.py"])
    s.events.append(Event.snapshot({"phase": "start"}, _ts(0)))
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, ["src/auth.py"], is_test=True, tool="pytest")
    )
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_PASSED, _ts(5), 0, ["src/auth.py"], is_test=True, is_verification=True, tool="pytest")
    )
    s.events.append(_cmd("echo done", COMMAND_STATUS_PASSED, _ts(10), 0))
    d = derive(s)
    assert d.one_liner.startswith("Failing check `pytest` passed")
    assert "after 3 attempts" in d.one_liner
    assert "over" in d.one_liner
    assert "changes touched src/auth.py" in d.one_liner


def test_one_liner_no_test():
    s = _session(is_repo=False, start=0, end=5)
    s.events.append(_cmd("echo hi", COMMAND_STATUS_PASSED, _ts(0), 0))
    s.events.append(Event.snapshot({"phase": "end"}, _ts(5)))
    d = derive(s)
    assert "no verification commands were run" in d.one_liner
    assert "1 command attempt" in d.one_liner
    assert "passed after" not in d.one_liner


def test_one_liner_failed_but_never_passed():
    s = _session(modified=["m.py"])
    s.events.append(_cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest"))
    s.events.append(_cmd("pytest", COMMAND_STATUS_FAILED, _ts(5), 1, is_test=True, tool="pytest"))
    d = derive(s)
    assert "failed and none passed" in d.one_liner
    assert "passed after" not in d.one_liner
    assert "Failing check" not in d.one_liner


def test_one_liner_notes_only():
    s = _session(start=0, end=8)
    s.events.append(Event.note("thinking", _ts(0)))
    s.events.append(Event.snapshot({"phase": "end"}, _ts(8)))
    d = derive(s)
    assert "no commands were run" in d.one_liner


def test_malformed_verification_timestamp_cannot_prove_success():
    s = _session()
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_PASSED, "not-a-timestamp", 0, is_test=True, is_verification=True, tool="pytest")
    )
    d = derive(s)
    assert d.proof_state == "unverified"
    assert d.verify_command is None
    assert d.one_liner == "Recorded 1 command attempt; verification `pytest` has no valid timestamp."


def test_malformed_later_failure_blocks_an_earlier_valid_pass():
    s = _session()
    s.events.append(_cmd("pytest", COMMAND_STATUS_PASSED, _ts(0), 0, is_test=True, is_verification=True, tool="pytest"))
    s.events.append(_cmd("pytest", COMMAND_STATUS_FAILED, "not-a-timestamp", 1, is_test=True, tool="pytest"))
    d = derive(s)
    assert d.proof_state == "unverified"
    assert d.invalid_verification_commands == ["pytest"]


def test_latest_failure_invalidates_a_historical_pass_in_summaries():
    s = _session()
    s.events.append(_cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest"))
    s.events.append(_cmd("pytest", COMMAND_STATUS_PASSED, _ts(5), 0, is_test=True, is_verification=True, tool="pytest"))
    s.events.append(_cmd("pytest", COMMAND_STATUS_FAILED, _ts(10), 1, is_test=True, tool="pytest"))
    d = derive(s)
    assert "latest recorded run still failed" in d.one_liner
    assert d.proof_state == "unverified"


# red to green -------------------------------------------------------------
def test_proof_state_tracks_the_latest_check_outcome():
    s = _session()
    s.events.append(
        _cmd(
            "pytest",
            COMMAND_STATUS_PASSED,
            _ts(0),
            0,
            is_test=True,
            is_verification=True,
            tool="pytest",
        )
    )
    s.events.append(
        _cmd(
            "pytest",
            COMMAND_STATUS_FAILED,
            _ts(30),
            1,
            is_test=True,
            tool="pytest",
        )
    )
    derivation = derive(s)
    assert derivation.proof_state == "unverified"
    assert "still failing" in derivation.proof_basis


def test_red_to_green_compares_structured_command_whitespace_exactly():
    s = _session()
    command = "python -c \"print('a  b')\""
    s.events.append(_cmd(command, COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest"))
    s.events.append(_cmd(command, COMMAND_STATUS_PASSED, _ts(5), 0, is_test=True, is_verification=True, tool="pytest"))
    assert derive(s).red_to_green is not None


def test_red_to_green_detected_with_files():
    s = _session(modified=["a.py", "b.py"])
    s.events.append(_cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, ["a.py"], is_test=True, tool="pytest"))
    s.events.append(_cmd("pytest", COMMAND_STATUS_PASSED, _ts(30), 0, ["a.py", "b.py"], is_test=True, is_verification=True, tool="pytest"))
    d = derive(s)
    assert d.red_to_green is not None
    assert d.red_to_green.command == "pytest"
    assert d.red_to_green.window_seconds == 30
    assert d.red_to_green.changed_files == ["a.py", "b.py"]


def test_red_to_green_records_a_commit_boundary_when_snapshots_have_no_file_changes():
    s = _session()
    failure = _cmd(
        "pytest",
        COMMAND_STATUS_FAILED,
        _ts(0),
        1,
        is_test=True,
        tool="pytest",
    )
    failure.data["git_head"] = "commit-a"
    passed = _cmd(
        "pytest",
        COMMAND_STATUS_PASSED,
        _ts(5),
        0,
        is_test=True,
        is_verification=True,
        tool="pytest",
    )
    passed.data["git_head"] = "commit-b"
    s.events.extend([failure, passed])
    result = derive(s).red_to_green
    assert result is not None
    assert result.commit_boundary is True
    assert result.changed_files == []


def test_red_to_green_records_a_first_commit_boundary_from_an_unborn_repo():
    s = _session()
    failure = _cmd(
        "pytest", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest"
    )
    passed = _cmd(
        "pytest",
        COMMAND_STATUS_PASSED,
        _ts(5),
        0,
        is_test=True,
        is_verification=True,
        tool="pytest",
    )
    passed.data["git_head"] = "first-commit"
    s.events.extend([failure, passed])
    result = derive(s).red_to_green
    assert result is not None
    assert result.commit_boundary is True


def test_red_to_green_includes_a_pre_existing_file_changed_during_the_window():
    s = _session(modified=["old-dirty.py"])
    s.git.initial_dirty = {"old-dirty.py": "baseline"}
    failure = _cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, ["old-dirty.py"], is_test=True, tool="pytest")
    failure.data["git_fingerprints"] = {"old-dirty.py": "changed"}
    s.events.append(failure)
    s.events.append(_cmd("pytest", COMMAND_STATUS_PASSED, _ts(5), 0, ["old-dirty.py"], is_test=True, is_verification=True, tool="pytest"))
    assert derive(s).red_to_green.changed_files == ["old-dirty.py"]


def test_red_to_green_excludes_pre_existing_dirty_files():
    s = _session(modified=["new.py"])
    s.git.initial_dirty = {"old-dirty.py": "baseline"}
    s.events.append(
        _cmd(
            "pytest",
            COMMAND_STATUS_FAILED,
            _ts(0),
            1,
            ["old-dirty.py", "new.py"],
            is_test=True,
            tool="pytest",
        )
    )
    s.events.append(
        _cmd(
            "pytest",
            COMMAND_STATUS_PASSED,
            _ts(5),
            0,
            ["old-dirty.py", "new.py"],
            is_test=True,
            is_verification=True,
            tool="pytest",
        )
    )
    assert derive(s).red_to_green.changed_files == ["new.py"]


def test_red_to_green_excludes_files_seen_only_on_the_failure_snapshot():
    s = _session()
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, ["before.py"], is_test=True, tool="pytest")
    )
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_PASSED, _ts(5), 0, ["during.py"], is_test=True, is_verification=True, tool="pytest")
    )
    assert derive(s).red_to_green.changed_files == ["during.py"]


def test_red_to_green_keeps_a_change_reverted_before_end():
    s = _session(modified=[])
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, ["temp.py"], is_test=True, tool="pytest")
    )
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_PASSED, _ts(5), 0, ["temp.py"], is_test=True, is_verification=True, tool="pytest")
    )
    assert derive(s).red_to_green.changed_files == ["temp.py"]


def test_red_to_green_no_transition():
    s = _session(modified=["a.py"])
    # Only passes, never a prior failure.
    s.events.append(_cmd("pytest", COMMAND_STATUS_PASSED, _ts(0), 0, ["a.py"], is_test=True, is_verification=True, tool="pytest"))
    d = derive(s)
    assert d.red_to_green is None


def test_red_to_green_omitted_outside_repo():
    s = _session(is_repo=False)
    s.events.append(_cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest"))
    s.events.append(_cmd("pytest", COMMAND_STATUS_PASSED, _ts(5), 0, is_test=True, is_verification=True, tool="pytest"))
    d = derive(s)
    assert d.red_to_green is None


# reproduce / verify -------------------------------------------------------
def test_reproduce_and_verify_selection():
    s = _session()
    s.events.append(_cmd("pytest -k a", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest"))
    s.events.append(_cmd("pytest -k a", COMMAND_STATUS_PASSED, _ts(5), 0, is_test=True, is_verification=True, tool="pytest"))
    d = derive(s)
    assert d.reproduce_command == "pytest -k a"
    assert d.verify_command == "pytest -k a"


def test_verify_none_when_nothing_passes():
    s = _session()
    s.events.append(_cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest"))
    d = derive(s)
    assert d.reproduce_command == "pytest"
    assert d.verify_command is None


# observed error -----------------------------------------------------------
def test_currently_failing_reflects_latest_outcome_not_history():
    from debugbrief.reporters import build_context

    s = _session()
    # The same check fails, then (well outside the dedup window) passes.
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest")
    )
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_PASSED, _ts(120), 0, is_test=True, is_verification=True, tool="pytest")
    )
    ctx = build_context(s)
    # The latest run passed, so nothing is currently failing, even though a
    # historical failure exists.
    assert ctx.currently_failing == []


def test_red_to_green_requires_the_same_directory():
    # The same command string run in two different directories (a monorepo) is
    # not the same check; a pass in one package is not the other's fix.
    s = _session()
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest",
             invocation_cwd="/repo/packages/a")
    )
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_PASSED, _ts(1), 0, is_test=True, is_verification=True,
             tool="pytest", invocation_cwd="/repo/packages/b")
    )
    assert derive(s).red_to_green is None


def test_red_to_green_requires_the_same_command():
    # A different check passing later is not the failing check turning green.
    s = _session()
    s.events.append(
        _cmd("pytest test_a.py", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest")
    )
    s.events.append(
        _cmd("ruff check .", COMMAND_STATUS_PASSED, _ts(1), 0, tool="ruff", is_verification=True)
    )
    assert derive(s).red_to_green is None


def test_red_to_green_pairs_the_same_command():
    s = _session()
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest")
    )
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_PASSED, _ts(1), 0, is_test=True, is_verification=True, tool="pytest")
    )
    r2g = derive(s).red_to_green
    assert r2g is not None and r2g.command == "pytest"


def test_observed_error_prefers_error_line():
    s = _session()
    s.events.append(
        _cmd(
            "pytest",
            COMMAND_STATUS_FAILED,
            _ts(0),
            1,
            is_test=True,
            tool="pytest",
            stderr="some noise\nAssertionError: boom\ntrailing noise",
        )
    )
    d = derive(s)
    assert d.observed_error == "trailing noise" or "AssertionError: boom" in d.observed_error
    # The error-like line is preferred over the final trailing line.
    assert d.observed_error == "AssertionError: boom"


def test_observed_error_none_without_output():
    s = _session()
    s.events.append(_cmd("pytest", COMMAND_STATUS_FAILED, _ts(0), 1, is_test=True, tool="pytest"))
    d = derive(s)
    assert d.observed_error is None


def test_observed_error_falls_back_to_stdout():
    # pytest prints assertion failures to stdout, not stderr.
    s = _session()
    s.events.append(
        _cmd(
            "python -m pytest -q",
            COMMAND_STATUS_FAILED,
            _ts(0),
            1,
            is_test=True,
            tool="pytest",
            stderr="",  # empty stderr, like a real failing pytest run
            stdout="collected 1 item\nFAILED test_x.py::test_add - AssertionError: boom\n1 failed",
        )
    )
    d = derive(s)
    assert d.observed_error == "FAILED test_x.py::test_add - AssertionError: boom"


def test_observed_error_prefers_stderr_over_stdout():
    s = _session()
    s.events.append(
        _cmd(
            "pytest",
            COMMAND_STATUS_FAILED,
            _ts(0),
            1,
            is_test=True,
            tool="pytest",
            stderr="real error on stderr",
            stdout="noise on stdout",
        )
    )
    d = derive(s)
    assert d.observed_error == "real error on stderr"


def test_observed_error_prefers_higher_priority_command_over_lower_stderr():
    # The failing verification command (pytest) prints its error only to stdout,
    # while an unrelated lower-priority command fails with output on stderr. The
    # primary command's error must win even though it is only on stdout; the
    # source preference (stderr over stdout) must not override command priority.
    s = _session()
    s.events.append(
        _cmd(
            "python -m pytest -q",
            COMMAND_STATUS_FAILED,
            _ts(0),
            1,
            is_test=True,
            tool="pytest",
            stderr="",
            stdout="collected 1 item\nFAILED test_x.py::test_add - AssertionError: boom\n1 failed",
        )
    )
    s.events.append(
        _cmd("ls /nope", COMMAND_STATUS_FAILED, _ts(1), 2, stderr="ls: /nope: No such file or directory")
    )
    d = derive(s)
    assert d.observed_error == "FAILED test_x.py::test_add - AssertionError: boom"


def test_observed_error_ignores_passing_command_output():
    s = _session()
    s.events.append(
        _cmd("pytest", COMMAND_STATUS_PASSED, _ts(0), 0, is_test=True, tool="pytest",
             stdout="AssertionError: this passed so must not be shown")
    )
    d = derive(s)
    assert d.observed_error is None


# timeline ordering --------------------------------------------------------
def test_timeline_orders_mixed_out_of_order_events():
    s = _session()
    # Append out of chronological order on purpose.
    s.events.append(_cmd("third", COMMAND_STATUS_PASSED, _ts(20), 0))
    s.events.append(Event.note("first", _ts(0)))
    s.events.append(_cmd("second", COMMAND_STATUS_FAILED, _ts(10), 1))
    ctx = build_context(s)
    texts = [e.text for e in ctx.timeline]
    assert texts[0] == "first"
    assert "second" in texts[1]
    assert "third" in texts[2]


# ruled out ----------------------------------------------------------------
def test_ruled_out_lists_failures_in_order():
    s = _session()
    s.events.append(_cmd("good", COMMAND_STATUS_PASSED, _ts(0), 0))
    s.events.append(_cmd("bad-one", COMMAND_STATUS_FAILED, _ts(5), 1))
    s.events.append(_cmd("bad-two", COMMAND_STATUS_FAILED, _ts(10), 2))
    d = derive(s)
    commands = [r.command for r in d.ruled_out]
    assert commands == ["bad-one", "bad-two"]


# next-step notes ----------------------------------------------------------
def test_next_step_notes_word_boundary():
    s = _session()
    s.events.append(Event.note("Token refresh fails when two requests retry.", _ts(0)))
    s.events.append(Event.note("TODO: investigate the lock ordering.", _ts(1)))
    steps = next_step_notes(s)
    # "retry" must not be mistaken for the hint "try".
    assert steps == ["TODO: investigate the lock ordering."]
