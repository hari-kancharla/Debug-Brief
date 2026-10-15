"""Command-line interface for DebugBrief (argparse only).

Commands:
    debugbrief start "<title>"
    debugbrief note  <text ...>
    debugbrief run   [--shell] [--timeout N] [--no-redact] -- <command ...>
    debugbrief run   "<command>"
    debugbrief redo  [--timeout N] [--no-redact] [--verify] [--dry-run] [--yes]
    debugbrief preview [--mode pr|handoff|incident] [--format md|json|html] [--include-full-path]
    debugbrief end   [--mode pr|handoff|incident] [--format md|json|html|both|all] [--stdout]
                     [--open] [--include-full-path]
    debugbrief cancel [--yes]
    debugbrief status
    debugbrief doctor [--fix]
    debugbrief last
    debugbrief open  [--last | --path PATH] [--html]
    debugbrief list  [--json]
    debugbrief show  <session_id> [--json]

run and note auto-start a session if none is active.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shlex
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import __version__, git_utils
from .command_runner import DEFAULT_TIMEOUT_SECONDS, RunResult, run_command
from .config import load_config
from .doctor import FAIL, run_doctor
from .markdown import normalize_free_text
from .models import (
    COMMAND_STATUS_PASSED,
    CommandData,
    effective_event_timestamp,
    valid_timestamp,
)
from .paths import UnsafeStateDirectory, ensure_local_ignore, resolve_project_paths
from .redaction import PLACEHOLDER
from .redo_safety import confirm_redo, is_destructive_command
from .reporters import (
    VALID_MODES,
    build_context,
    render_report,
    render_report_html,
    render_report_json,
)
from .reports_index import first_title, infer_mode, latest_report, list_reports
from .session_manager import SessionError, SessionManager
from .sessions_index import (
    is_verified,
    load_all_sessions,
    normalize_session_counts,
    report_modes_for,
    resolve_session_id,
)
from .utils import eprint, is_regular_file, is_supported_platform, parse_iso8601

PROG = "debugbrief"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Record the meaningful context of a debugging session and turn it "
            "into an evidence-backed report (PR, handoff, or incident)."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # start --------------------------------------------------------------
    p_init = subparsers.add_parser(
        "init", help="Set up DebugBrief in this project and show how to use it."
    )
    p_init.set_defaults(func=cmd_init)

    p_start = subparsers.add_parser("start", help="Start a new debugging session.")
    p_start.add_argument("title", help="A short, descriptive session title.")
    # Experimental shell-history capture is not available in v1. Keep the flag so
    # `start --shell` still prints a helpful message, but hide it from normal help.
    p_start.add_argument(
        "--shell",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p_start.set_defaults(func=cmd_start)

    # note ---------------------------------------------------------------
    p_note = subparsers.add_parser("note", help="Append a note to the active session.")
    p_note.add_argument(
        "text",
        nargs="+",
        help=(
            "The note text. Quoting is optional: "
            "debugbrief note remember to check the lock ordering"
        ),
    )
    p_note.set_defaults(func=cmd_note)

    # run ----------------------------------------------------------------
    p_run = subparsers.add_parser(
        "run",
        help="Execute a command, capturing its outcome into the active session.",
    )
    p_run.add_argument(
        "--shell",
        action="store_true",
        help=(
            "Run the command through the shell (enables pipes, redirection, &&). "
            "Uses bash with pipefail when bash is available. A compound command is "
            "recorded as one command, not attributed to an internal tool; declare "
            "it a check with --verify. Default parses with shlex, no shell."
        ),
    )
    p_run.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help=f"Kill the command after this many seconds (default {DEFAULT_TIMEOUT_SECONDS}).",
    )
    p_run.add_argument(
        "--no-redact",
        dest="no_redact",
        action="store_true",
        help=(
            "Store captured output and the command verbatim, without secret "
            "redaction. Use only when you know the output is safe."
        ),
    )
    p_run.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Declare this command a check (custom test script, make "
            "integration). It counts as verification when it exits 0; a "
            "recognized test runner is classified automatically and wins."
        ),
    )
    p_run.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help=(
            "The command to run. Put DebugBrief flags first, then -- and the "
            "command as you would normally type it: "
            "debugbrief run -- python -m pytest -q tests/. "
            'A single quoted argument also works: debugbrief run "pytest -q".'
        ),
    )
    p_run.set_defaults(func=cmd_run)

    # redo ---------------------------------------------------------------
    p_redo = subparsers.add_parser(
        "redo",
        help="Re-run the most recently captured command in the active session.",
    )
    p_redo.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help=f"Kill the command after this many seconds (default {DEFAULT_TIMEOUT_SECONDS}).",
    )
    p_redo.add_argument(
        "--no-redact",
        dest="no_redact",
        action="store_true",
        help="Store captured output verbatim, without secret redaction.",
    )
    p_redo.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Declare the re-run a check (see run --verify). Inherited "
            "automatically when the original run was declared with --verify."
        ),
    )
    p_redo.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command that would be re-run without executing it.",
    )
    p_redo.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation for a destructive re-run.",
    )
    p_redo.set_defaults(func=cmd_redo)

    # preview ------------------------------------------------------------
    p_preview = subparsers.add_parser(
        "preview",
        help="Print the report for the active session without ending it.",
    )
    p_preview.add_argument(
        "--mode",
        default=None,
        choices=VALID_MODES,
        help="Report style to preview (default pr).",
    )
    p_preview.add_argument(
        "--detail",
        choices=["full", "compact"],
        default=None,
        help="Report verbosity: compact (default) for PR, or full for the complete record.",
    )
    p_preview.add_argument(
        "--format",
        dest="report_format",
        choices=["md", "json", "html"],
        default="md",
        help="Preview format: md (default), json, or html.",
    )
    preview_path = p_preview.add_mutually_exclusive_group()
    preview_path.add_argument(
        "--include-full-path",
        dest="include_full_path",
        action="store_true",
        default=None,
        help="Include the absolute project root in the generated report.",
    )
    preview_path.add_argument(
        "--no-include-full-path",
        dest="include_full_path",
        action="store_false",
        default=None,
        help="Keep the absolute project root out of the generated report.",
    )
    p_preview.set_defaults(func=cmd_preview)

    # end ----------------------------------------------------------------
    p_end = subparsers.add_parser(
        "end", help="Finalize the session and write a report artifact."
    )
    p_end.add_argument(
        "--mode",
        default=None,
        choices=VALID_MODES,
        help="Report style to generate (default pr).",
    )
    p_end.add_argument(
        "--format",
        dest="report_format",
        choices=["md", "json", "html", "both", "all"],
        default="md",
        help="Report output: md, json, html, both, or all (default md).",
    )
    p_end.add_argument(
        "--stdout",
        dest="to_stdout",
        action="store_true",
        help=(
            "Print the selected report to stdout (the file is still written). "
            "Informational lines move to stderr, so Markdown output pipes cleanly: "
            "debugbrief end --stdout | gh pr comment --body-file -"
        ),
    )
    p_end.add_argument(
        "--open",
        action="store_true",
        help=(
            "Open the generated HTML report in the system browser "
            "(use with --format html or all)."
        ),
    )
    p_end.add_argument(
        "--detail",
        choices=["full", "compact"],
        default=None,
        help="Report verbosity: compact (default) for PR, or full for the complete record.",
    )
    end_path = p_end.add_mutually_exclusive_group()
    end_path.add_argument(
        "--include-full-path",
        dest="include_full_path",
        action="store_true",
        default=None,
        help="Include the absolute project root in the generated report.",
    )
    end_path.add_argument(
        "--no-include-full-path",
        dest="include_full_path",
        action="store_false",
        default=None,
        help="Keep the absolute project root out of the generated report.",
    )
    p_end.set_defaults(func=cmd_end)

    # cancel -------------------------------------------------------------
    p_cancel = subparsers.add_parser(
        "cancel",
        help="Abandon the active session without writing a report.",
    )
    p_cancel.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    p_cancel.set_defaults(func=cmd_cancel)

    # status -------------------------------------------------------------
    p_status = subparsers.add_parser(
        "status", help="Show the active session and any running captured command."
    )
    p_status.set_defaults(func=cmd_status)

    # doctor -------------------------------------------------------------
    p_doctor = subparsers.add_parser(
        "doctor", help="Run a health check on the project and DebugBrief state."
    )
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Apply safe fixes: create .debugbrief/ directories and add "
            ".debugbrief/ to .git/info/exclude. Never touches .gitignore."
        ),
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_recover = subparsers.add_parser(
        "recover",
        help="Repair a broken or stale active-session pointer after a crash.",
    )
    p_recover.set_defaults(func=cmd_recover)

    # last ---------------------------------------------------------------
    p_last = subparsers.add_parser(
        "last", help="Show the most recently generated report."
    )
    p_last.add_argument(
        "--open", action="store_true", help="Open the latest report immediately."
    )
    p_last.set_defaults(func=cmd_last)

    # open ---------------------------------------------------------------
    p_open = subparsers.add_parser(
        "open",
        help="Open a report in $EDITOR or the system browser (defaults to the latest report).",
    )
    open_target = p_open.add_mutually_exclusive_group()
    open_target.add_argument(
        "--last", action="store_true", help="Open the latest report (default)."
    )
    open_target.add_argument(
        "--path", metavar="REPORT", help="Open a specific report file."
    )
    p_open.add_argument(
        "--html",
        action="store_true",
        help="Open the latest HTML report in the system browser instead of an editor.",
    )
    p_open.set_defaults(func=cmd_open)

    # list ---------------------------------------------------------------
    p_list = subparsers.add_parser(
        "list", help="List recorded sessions, most recent first."
    )
    p_list.add_argument(
        "--json", action="store_true", help="Emit a structured JSON summary."
    )
    p_list.set_defaults(func=cmd_list)

    # show ---------------------------------------------------------------
    p_show = subparsers.add_parser(
        "show", help="Show a compact summary of one session."
    )
    p_show.add_argument(
        "session_id", help="Full or short (unambiguous prefix) session id."
    )
    p_show.add_argument(
        "--json", action="store_true", help="Emit the full session as JSON."
    )
    p_show.set_defaults(func=cmd_show)

    return parser


def _manager() -> SessionManager:
    paths = resolve_project_paths()
    return SessionManager(paths)


def _apply_local_ignore(manager, paths, session):
    """Ensure .debugbrief/ is locally ignored; record any warning on the session."""
    changed, warnings = ensure_local_ignore(paths)
    if warnings:
        from .utils import now_iso8601

        for warning in warnings:
            session.add_warning(warning, now_iso8601())
        manager.save_session(session)
    return changed, warnings


def _ensure_session(manager, paths, seed_text):
    """Return the active session, auto-starting one if none is active.

    Auto-start prints a clear one-line notice and continues, so a note or a
    command run is never silently dropped.
    """
    if manager.has_active():
        return manager.load_active()
    session = manager.auto_start(seed_text)
    changed, warnings = _apply_local_ignore(manager, paths, session)
    # Status lines go to stderr so a wrapped command's stdout stays clean.
    eprint(
        "Auto-started a DebugBrief session (none was active): "
        f"{_display(session.title)}"
    )
    eprint(f"  id: {_display(session.session_id)}")
    if changed:
        eprint("  ignore: added .debugbrief/ to .git/info/exclude")
    for warning in warnings:
        eprint(f"  warning: {_display(warning)}")
    return session


# Handlers -----------------------------------------------------------------
def cmd_start(args: argparse.Namespace) -> int:
    if args.shell:
        eprint(
            "Experimental shell-history capture (start --shell) is not available "
            "in v1.\n"
            "The reliable capture model is explicit execution:\n"
            '  debugbrief run "<command>"\n'
            "Start a normal session with: debugbrief start \"<title>\""
        )
        return 2

    paths = resolve_project_paths()
    manager = SessionManager(paths)
    session = manager.start(args.title)

    # Locally ignore .debugbrief/ via .git/info/exclude (never touches .gitignore).
    changed, warnings = _apply_local_ignore(manager, paths, session)

    print("Started DebugBrief session.")
    print(f"  id:        {_display(session.session_id)}")
    print(f"  title:     {_display(session.title)}")
    print(f"  root:      {_display(session.project_root)}")
    if session.git.is_repo:
        branch = session.git.branch or (
            "(detached HEAD)" if session.git.detached_head else "(no branch)"
        )
        print(f"  git:       repo on {_display(branch)}")
    else:
        print("  git:       not a Git repository (continuing locally)")
    if changed:
        print("  ignore:    added .debugbrief/ to .git/info/exclude")
    for warning in warnings:
        eprint(f"  warning:   {_display(warning)}")
    print("")
    print("Next:")
    print('  debugbrief note "<observation>"')
    print("  debugbrief run  -- <command>")
    print("  debugbrief redo")
    print("  debugbrief end  [--mode pr|handoff|incident]")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    # Unquoted notes arrive as multiple tokens; a lossy single-space join is
    # fine for prose. The quoted single-argument form passes through verbatim.
    text = args.text if isinstance(args.text, str) else " ".join(args.text)
    paths = resolve_project_paths()
    manager = SessionManager(paths)
    _ensure_session(manager, paths, text)
    session = manager.add_note(text)
    print(
        f"Noted locally · {session.summary.notes_count} note"
        f"{'s' if session.summary.notes_count != 1 else ''} · "
        f"session {session.session_id[:8]}"
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    command_str = _reconstruct_command(args.command)
    if not command_str.strip():
        eprint(
            "No command given. Usage: debugbrief run [flags] -- <command ...> "
            'or debugbrief run "<command>"'
        )
        return 2

    paths = resolve_project_paths()
    manager = SessionManager(paths)

    if args.timeout <= 0:
        eprint("--timeout must be a positive number of seconds.")
        return 2

    # Auto-start a session if none is active so the run is never dropped. The
    # title seed uses the plain tokens as typed, not the shlex-quoted
    # reconstruction, so auto titles read naturally in list and reports.
    _ensure_session(manager, paths, _plain_command_text(args.command))

    # The command's own stdout/stderr stream through live while it runs.
    # DebugBrief's status lines all go to stderr so the wrapped command's
    # stdout stays clean for piping.
    # Run from the directory the user is actually in, not the repo root, so
    # commands behave the same as typing them directly (important in monorepos
    # and subdirectories). State still lives at the repo root.
    invocation_cwd = Path.cwd()
    # Hold the active-command lease for the command's lifetime so end/cancel and a
    # second run cannot run concurrently with it.
    with manager.command_lease(command_str, str(invocation_cwd)) as command_id:
        eprint(f"$ {_display(command_str)}")
        result = run_command(
            command=command_str,
            cwd=invocation_cwd,
            use_shell=args.shell,
            timeout_seconds=args.timeout,
            redact=not args.no_redact,
            force_verification=args.verify,
            pass_fds=manager.command_pass_fds,
        )
        result.command_data.invocation_cwd = str(invocation_cwd)
        with _deferred_sigint():
            manager.record_command(result, command_id=command_id)
    _print_command_outcome(result, args.timeout)
    return result.propagated_exit_code


def _print_command_outcome(result: RunResult, timeout_seconds: int) -> None:
    """Report a captured command's outcome on stderr (shared by run and redo)."""
    data = result.command_data
    if result.errored:
        eprint(f"  error:     {_display(result.error_message)}")
    elif result.interrupted:
        eprint("  status:    interrupted (recorded)")
    elif result.broken_pipe:
        eprint("  status:    stopped, downstream pipe closed (recorded)")
    elif result.timed_out:
        eprint(f"  status:    timed out after {timeout_seconds}s (recorded)")
    else:
        verdict = "passed" if data.classification.status == COMMAND_STATUS_PASSED else "failed"
        eprint(f"  status:    {verdict} (exit {data.exit_code})")
    eprint("  captured:  locally · bounded preview · Git state when available")
    eprint(f"  duration:  {data.duration_seconds}s")
    if data.classification.is_test:
        eprint(f"  test:      {_display(data.classification.tool or 'unknown')}")
    if data.stdout_truncated:
        eprint("  note:      stdout preview was truncated")
    if data.stderr_truncated:
        eprint("  note:      stderr preview was truncated")
    if data.redacted:
        eprint("  note:      secret-like values were redacted")
    if result.warning:
        eprint(f"  warning:   {_display(result.warning)}")
    if data.classification.status == COMMAND_STATUS_PASSED:
        eprint("  next:      debugbrief end --format html")
    else:
        eprint("  next:      edit, then debugbrief redo")


def _latest_command_event(events):
    ranked = []
    for index, event in enumerate(events):
        stamp = valid_timestamp(effective_event_timestamp(event))
        if stamp is None:
            continue
        try:
            seconds = parse_iso8601(stamp).timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
        ranked.append((seconds, index, event))
    if ranked:
        selected = max(ranked, key=lambda item: (item[0], item[1]))
        selected_index = selected[1]
        for index, event in enumerate(events):
            data = event.data if isinstance(event.data, dict) else {}
            ended_at = data.get("ended_at")
            if (
                index > selected_index
                and "ended_at" in data
                and valid_timestamp(ended_at) is None
            ):
                selected = (0.0, index, event)
                selected_index = index
        return selected[2]
    if events:
        return events[-1]
    raise ValueError("no command events")


def _redo_preflight(manager, paths, session, check_active=False):
    if check_active:
        try:
            active = manager._command_is_active()
        except Exception:
            return None, None, "DebugBrief could not determine command activity safely."
        if active:
            return None, None, "A captured command is already running in this project."
    events = session.command_events()
    if not events:
        return None, None, "No commands have been captured in this session yet."
    selected = _latest_command_event(events)
    selected_index = next(
        index for index, event in enumerate(events) if event is selected
    )
    for index, event in enumerate(events):
        if index > selected_index and not valid_timestamp(
            effective_event_timestamp(event)
        ):
            return None, None, (
                "A newer command has no valid timestamp, so redo cannot select a "
                "safe command."
            )
    if not valid_timestamp(effective_event_timestamp(selected)):
        return None, None, "The selected command has no valid timestamp."
    try:
        state = git_utils.tracked_state(
            paths.project_root, paths.session_file(session.session_id)
        )
    except Exception:
        state = git_utils.TrackedState.UNKNOWN
    if state is git_utils.TrackedState.TRACKED:
        return None, None, (
            "The active session is stored in a file tracked by Git, so it may "
            "have come from the repository rather than your own runs. Remove it "
            "from the index (git rm --cached -r .debugbrief) and run the command "
            "yourself: debugbrief run -- <command>"
        )
    if state is not git_utils.TrackedState.UNTRACKED:
        return None, None, (
            "DebugBrief could not confirm whether this session file is tracked by "
            "Git, so redo was refused for safety. Run the command manually with: "
            "debugbrief run -- <command>"
        )
    seed = CommandData.from_dict(selected.data)
    if not seed.command.strip():
        return None, None, "The selected command is empty and cannot be re-run."
    if PLACEHOLDER in seed.command:
        return None, None, (
            f"The last stored command contains {PLACEHOLDER}, a redaction "
            "placeholder, not the real text, so it cannot be re-run. Run the "
            "command again yourself: debugbrief run -- <command>"
        )
    if seed.invocation_cwd:
        try:
            invocation_cwd = Path(seed.invocation_cwd)
            valid_cwd = invocation_cwd.is_dir()
        except (OSError, TypeError, ValueError):
            invocation_cwd = Path.cwd()
            valid_cwd = False
        if not valid_cwd:
            return None, None, (
                f"The original command ran in {seed.invocation_cwd}, which no "
                "longer exists, so redo did not run it. Re-run it yourself from a "
                "valid directory: debugbrief run -- <command>"
            )
    else:
        invocation_cwd = Path.cwd()
    return seed, invocation_cwd, None


def _redo_refusal(reason: str, dry_run: bool = False) -> int:
    prefix = "Redo preflight: normal redo would be refused: " if dry_run else ""
    eprint(prefix + _display(reason))
    return 1


def cmd_redo(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()
    manager = SessionManager(paths)
    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_SECONDS

    if timeout <= 0:
        eprint("--timeout must be a positive number of seconds.")
        return 2

    session = manager.load_active()
    if session is None:
        eprint(
            "No active DebugBrief session, so there is nothing to redo. "
            "Run a command first: debugbrief run -- <command>"
        )
        return 1

    if getattr(args, "dry_run", False):
        seed, invocation_cwd, reason = _redo_preflight(
            manager, paths, session, check_active=True
        )
        if reason is not None:
            return _redo_refusal(reason, dry_run=True)
        assert seed is not None and invocation_cwd is not None
        print(f"$ {normalize_free_text(seed.command)}  (redo --dry-run)")
        print(f"  cwd:       {_display(invocation_cwd)}")
        if not seed.invocation_cwd:
            eprint(
                "  warning:   the original command's directory was not recorded; "
                "the dry run would use the current directory."
            )
        if is_destructive_command(seed.command, used_shell=seed.used_shell):
            if getattr(args, "yes", False):
                print("  confirmation: bypassed by --yes for normal redo")
            else:
                print("  confirmation: normal redo would require confirmation")
        print("  normal redo would not be refused by preflight")
        return 0

    seed, seed_cwd, reason = _redo_preflight(
        manager, paths, session, check_active=True
    )
    if reason is not None:
        return _redo_refusal(reason)
    assert seed is not None and seed_cwd is not None
    with manager.command_lease(seed.command, str(seed_cwd)) as command_id:
        session = manager.load_active()
        if session is None:
            eprint("No active DebugBrief session, so there is nothing to redo.")
            return 1
        last, invocation_cwd, reason = _redo_preflight(
            manager, paths, session
        )
        if reason is not None:
            return _redo_refusal(reason)
        assert last is not None and invocation_cwd is not None
        inherit_verify = last.classification.tool == "custom"
        if not last.invocation_cwd:
            eprint(
                "  warning:   the original command's directory was not recorded; "
                "re-running in the current directory."
            )
        try:
            confirmed = confirm_redo(
                last.command,
                assume_yes=bool(getattr(args, "yes", False)),
                used_shell=last.used_shell,
            )
        except KeyboardInterrupt:
            return 1
        if not confirmed:
            return 1
        eprint(f"$ {normalize_free_text(last.command)}  (redo)")
        result = run_command(
            command=last.command,
            cwd=invocation_cwd,
            use_shell=last.used_shell,
            timeout_seconds=timeout,
            redact=not bool(getattr(args, "no_redact", False)),
            force_verification=bool(getattr(args, "verify", False)) or inherit_verify,
            pass_fds=manager.command_pass_fds,
        )
        result.command_data.invocation_cwd = str(invocation_cwd)
        with _deferred_sigint():
            manager.record_command(result, command_id=command_id)
    _print_command_outcome(result, timeout)
    return result.propagated_exit_code


def cmd_end(args: argparse.Namespace) -> int:
    manager = _manager()
    if getattr(args, "open", False) and args.report_format not in ("html", "all"):
        eprint("--open requires --format html or --format all.")
        return 2
    include_full_path = bool(getattr(args, "include_full_path", False))
    session = manager.end(
        args.mode,
        args.report_format,
        detail=args.detail,
        include_full_path=include_full_path,
    )
    # With --stdout the report itself owns stdout; everything informational
    # moves to stderr so the output pipes cleanly.
    info = eprint if args.to_stdout else print
    info(f"Session completed: {_display(session.title)}")
    info(f"  mode:      {args.mode}")
    if args.report_format in ("md", "both", "all"):
        info(
            f"  report:    {_display(manager.paths.report_file(session.session_id, args.mode))}"
        )
    if args.report_format in ("json", "both", "all"):
        info(
            f"  json:      "
            f"{_display(manager.paths.report_json_file(session.session_id, args.mode))}"
        )
    if args.report_format in ("html", "all"):
        info(
            f"  html:      "
            f"{_display(manager.paths.report_html_file(session.session_id, args.mode))}"
        )
    info(f"  session:   {_display(manager.paths.session_file(session.session_id))}")
    info("  proof:     saved locally · review before sharing")
    if args.report_format in ("html", "all"):
        info("  open:      debugbrief open --html")
    else:
        info("  open:      debugbrief open")
    if args.to_stdout:
        if args.report_format == "html":
            rendered = render_report_html(
                session, args.mode, include_full_path=include_full_path
            )
        elif args.report_format == "json":
            payload = render_report_json(
                session, args.mode, include_full_path=include_full_path
            )
            rendered = json.dumps(payload, indent=2) + "\n"
        else:
            rendered = (
                render_report(session, args.mode, args.detail, include_full_path=True)
                if include_full_path
                else render_report(session, args.mode, args.detail)
            )
        sys.stdout.write(rendered)
    if getattr(args, "open", False):
        return _open_report_in_browser(
            manager.paths.report_html_file(session.session_id, args.mode),
            announce_to_stderr=bool(getattr(args, "to_stdout", False)),
        )
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    manager = _manager()
    rendered = manager.preview(
        args.mode,
        detail=args.detail,
        include_full_path=bool(getattr(args, "include_full_path", False)),
        report_format=getattr(args, "report_format", "md"),
    )
    if getattr(args, "report_format", "md") != "md":
        sys.stdout.write(rendered)
        return 0
    banner = "_Preview of an active session. Run debugbrief end to finalize._"
    lines = rendered.splitlines()
    if lines and lines[0].startswith("# "):
        rendered = lines[0] + "\n\n" + banner + "\n" + "\n".join(lines[1:]) + "\n"
    else:  # pragma: no cover - reports always start with a title
        rendered = banner + "\n\n" + rendered
    sys.stdout.write(rendered)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    manager = _manager()
    session = manager.load_active()
    if session is None:
        eprint("No active DebugBrief session to cancel.")
        return 1

    if not args.yes:
        try:
            answer = input(f"Abandon active session '{_display(session.title)}'? [y/N] ")
        except EOFError:
            # No stdin to answer with (e.g. a pipe): treat as a decline.
            answer = ""
        if answer.strip().lower() != "y":
            eprint("Aborted; the session is still active.")
            return 1

    manager.cancel()
    eprint(
        f"Abandoned session '{_display(session.title)}'. No report written; "
        "the local record remains available to list/show."
    )
    return 0


_BACKGROUND_LOCK_NOTE = (
    "Note: a process started by an earlier command is still holding the command "
    "lock (it was backgrounded and has not exited). New commands, end, and cancel "
    "stay blocked until it exits."
)


def _display(value) -> str:
    return normalize_free_text("" if value is None else value)


def _print_active_command(status) -> None:
    command = status.get("active_command")
    if not isinstance(command, dict):
        return
    state = str(command.get("state", "unknown"))
    print(f"  command:  {state}")
    preview = command.get("command_preview")
    if isinstance(preview, str) and preview.strip():
        print(f"    {normalize_free_text(preview)}")
    started = command.get("started_at")
    if isinstance(started, str) and started.strip():
        print(f"    started: {normalize_free_text(started)}")
    cwd = command.get("invocation_cwd")
    if isinstance(cwd, str) and cwd.strip():
        print(f"    cwd:     {normalize_free_text(cwd)}")
    detail = command.get("detail")
    if isinstance(detail, str) and detail.strip():
        print(f"    detail:  {normalize_free_text(detail)}")
    if state in {"stale", "unsafe", "unreadable"}:
        print("    action:  run 'debugbrief recover' to repair the lease")


def cmd_status(args: argparse.Namespace) -> int:
    manager = _manager()
    status = manager.build_status()

    if not status.get("active"):
        print("No active DebugBrief session.")
        print('Start one with: debugbrief start "<title>"')
        if status.get("background_lock"):
            print("")
            print(_BACKGROUND_LOCK_NOTE)
        _print_active_command(status)
        return 0

    if status.get("interrupted"):
        print("A session appears INTERRUPTED or inconsistent.")
        print(f"  id:        {_display(status.get('session_id'))}")
        if status.get("title"):
            print(f"  title:     {_display(status.get('title'))}")
        reason = status.get("reason")
        if reason:
            print(f"  reason:    {_display(reason)}")
        print("")
        print("Recovery:")
        print("  - Run 'debugbrief recover' to repair the pointer safely")
        print("    (it never deletes session data), then start a fresh session.")
        _print_active_command(status)
        return 0

    print(f"Active session: {_display(status.get('title'))}")
    print(f"  id:        {_display(status.get('session_id'))}")
    print(f"  status:    {_display(status.get('status'))}")
    print(f"  root:      {_display(status.get('project_root'))}")
    print(f"  started:   {_display(status.get('start'))}")
    print(f"  notes:     {status.get('notes_count')}")
    print(
        f"  commands:  {status.get('commands_count')} "
        f"({status.get('failed_commands_count')} failed)"
    )
    if status.get("is_repo"):
        branch = status.get("branch") or (
            "(detached HEAD)" if status.get("detached_head") else "(no branch)"
        )
        print(f"  git:       {_display(branch)}")
    else:
        print("  git:       not a Git repository")
    for warning in status.get("warnings", []):
        print(f"  warning:   {_display(warning)}")
    if status.get("background_lock"):
        print("")
        print(_BACKGROUND_LOCK_NOTE)
    _print_active_command(status)
    print("")
    print("End with: debugbrief end --mode pr|handoff|incident")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Set up DebugBrief in this project and explain how to use it.

    Onboarding only: it performs the same safe setup as ``doctor --fix`` (create
    ``.debugbrief/`` and ignore it locally), reports health, and prints the alias
    and workflow. It never starts a session or writes a report, so running it is
    side-effect-free beyond the storage directory.
    """
    import os as _os

    paths = resolve_project_paths()
    report = run_doctor(paths, fix=True)

    print("DebugBrief is set up for this project.")
    print(f"  project root:  {_display(paths.project_root)}")
    if paths.is_git_repo:
        print("  git:           repository detected (.debugbrief/ is ignored locally)")
    else:
        print("  git:           not a Git repository (Git sections are omitted)")
    print(f"  health:        {_display(report.summary)}")

    blocking = [c for c in report.checks if c.level == FAIL]
    if blocking:
        print("")
        print("  Needs attention (run 'debugbrief doctor' for the full report):")
        for check in blocking:
            print(f"    - {check.name}{': ' + check.detail if check.detail else ''}")

    shell = _os.environ.get("SHELL", "")
    rc_file = "~/.zshrc" if shell.endswith("zsh") else "~/.bashrc"
    print("")
    print("Make the capture prefix disappear with a one-line alias:")
    print('  alias db="debugbrief run --"')
    print(f"  Add that to {rc_file}, reload your shell, then run: db pytest -q")
    print("")
    print("Your loop from then on:")
    print('  debugbrief start "<what you are fixing>"   (optional; run/note auto-start)')
    print("  db <command>                               (run commands through DebugBrief)")
    print('  debugbrief note "<observation>"            (record findings as you go)')
    print("  debugbrief redo                            (re-run the last command)")
    print("  debugbrief end                             (write the brief)")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    manager = _manager()
    result = manager.recover()
    lease = result.get("lease", "none")
    if lease == "live":
        print("A captured command is still running; its lease was left untouched.")
    elif lease == "cleared_stale":
        print(
            "Cleared a stale command lease (its process had exited); the session "
            "was preserved."
        )
    elif lease == "unclearable":
        print(
            "A stale command lease could not be removed (it may be a directory). "
            "Remove .debugbrief/active_command.json manually."
        )
    elif lease == "held_by_background":
        print(
            "A process started by an earlier command is still holding the command "
            "lock (it was backgrounded and has not exited). New commands, end, and "
            "cancel stay blocked until it exits; there is no stale lease to clear."
        )
    action = result["action"]
    if action == "healthy":
        print(
            "Active session is healthy, nothing to recover: "
            f"{_display(result['detail'])}"
        )
    elif action == "cleared_broken_pointer":
        print("Cleared a broken active-session pointer so a new session can start.")
        print(f"  reason: {_display(result['detail'])}")
    elif action == "cleared_stale_pointer":
        print(f"Cleared a stale pointer to a {_display(result['detail'])} session.")
    else:
        print("No active-session pointer; nothing to recover.")
    corrupt = result["corrupt"]
    if corrupt:
        print("")
        print(f"Found {len(corrupt)} unreadable session file(s), left in place:")
        for name in corrupt:
            print(f"  - {_display(name)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()
    report = run_doctor(paths, fix=args.fix)
    print("DebugBrief doctor")
    print("=================")
    for check in report.checks:
        detail = f" - {_display(check.detail)}" if check.detail else ""
        print(f"[{check.level}] {_display(check.name)}{detail}")
    print("")
    print(_display(report.summary))
    return report.exit_code


def _open_report_in_browser(target: Path, announce_to_stderr: bool = False) -> int:
    try:
        opened = webbrowser.open(target.resolve().as_uri())
    except (OSError, ValueError) as exc:
        eprint(f"Could not open browser ({exc}).")
        eprint(f"Report path: {_display(target)}")
        return 1
    if not opened:
        eprint("The system browser could not be opened.")
        eprint(f"Report path: {_display(target)}")
        return 1
    message = f"Opening report: {_display(target)}"
    if announce_to_stderr:
        eprint(message)
    else:
        print(message)
    return 0


def _open_report_in_editor(target: Path) -> int:
    editor = os.environ.get("EDITOR", "").strip()
    if not editor:
        print(f"Report path: {_display(target)}")
        print(
            "Set the $EDITOR environment variable to open reports automatically, "
            'e.g. export EDITOR="vim" (or "code -w").'
        )
        return 0

    try:
        editor_args = shlex.split(editor)
    except ValueError:
        editor_args = [editor]

    try:
        completed = subprocess.run([*editor_args, str(target)])
    except (OSError, ValueError) as exc:
        eprint(f"Could not open editor ({exc}).")
        eprint(f"Report path: {_display(target)}")
        return 1

    if completed.returncode != 0:
        eprint(f"Editor exited with code {completed.returncode}.")
        eprint(f"Report path: {_display(target)}")
        return 1
    return 0


def cmd_last(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()
    paths.assert_state_dirs_safe()
    report_path = latest_report(paths.reports_dir)
    if report_path is None:
        eprint(
            "No DebugBrief reports found. Generate one with: "
            "debugbrief end --mode pr|handoff|incident"
        )
        return 1

    mode = infer_mode(report_path) or "unknown"
    title = first_title(report_path)
    try:
        mtime = datetime.fromtimestamp(report_path.stat().st_mtime)
    except (OSError, ValueError, OverflowError):
        eprint("The latest report could not be inspected safely.")
        return 1
    print(f"Latest report: {_display(report_path)}")
    print(f"  mode:      {mode}")
    print(f"  modified:  {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  title:     {_display(title or '(no title line found)')}")
    if getattr(args, "open", False):
        if report_path.suffix == ".html":
            return _open_report_in_browser(report_path)
        return _open_report_in_editor(report_path)
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()
    paths.assert_state_dirs_safe()

    target: Optional[Path]
    if args.path:
        target = Path(args.path).expanduser()
        if not os.path.lexists(target):
            eprint(f"Report not found: {_display(target)}")
            return 1
        if not is_regular_file(target):
            eprint(f"Report path is not a regular file: {_display(target)}")
            return 1
    elif getattr(args, "html", False):
        target = next(
            (path for path in list_reports(paths.reports_dir) if path.suffix == ".html"),
            None,
        )
        if target is None:
            eprint(
                "No HTML report found. Generate one with: "
                "debugbrief end --format html"
            )
            return 1
    else:
        target = latest_report(paths.reports_dir)
        if target is None:
            eprint(
                "No DebugBrief reports found to open. Generate one with: "
                "debugbrief end --mode pr|handoff|incident"
            )
            return 1

    if getattr(args, "html", False):
        if target.suffix != ".html":
            eprint(f"Expected an HTML report, got: {_display(target)}")
            return 1
        return _open_report_in_browser(target)
    if target.suffix == ".html":
        return _open_report_in_browser(target)
    return _open_report_in_editor(target)


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _json_session_payload(session) -> dict:
    payload = session.to_dict()
    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict) and event.get("type") == "command":
                data = event.get("data")
                event["data"] = CommandData.from_dict(
                    data if isinstance(data, dict) else {}
                ).to_dict()
    return _json_safe(payload)


def _session_summary_dict(paths, session) -> dict:
    modes = report_modes_for(paths, session.session_id)
    return {
        "session_id": session.session_id,
        "short_id": session.session_id[:8],
        "status": normalize_free_text(session.status),
        "title": normalize_free_text(session.title),
        "start": normalize_free_text(session.timestamps.start or ""),
        "end": normalize_free_text(session.timestamps.end or ""),
        "commands_count": session.summary.commands_count,
        "notes_count": session.summary.notes_count,
        "failed_commands_count": session.summary.failed_commands_count,
        "verified": is_verified(session),
        "report_modes": modes,
    }


def cmd_list(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()
    sessions = load_all_sessions(paths)

    if not sessions:
        if args.json:
            print("[]")
            return 1
        eprint(
            "No DebugBrief sessions found. Start one with: "
            'debugbrief start "<title>"'
        )
        return 1

    if args.json:
        payload = [_session_summary_dict(paths, s) for s in sessions]
        print(json.dumps(payload, indent=2))
        return 0

    print(f"{len(sessions)} session(s), most recent first:")
    print("")
    for session in sessions:
        modes = report_modes_for(paths, session.session_id)
        modes_label = ", ".join(modes) if modes else "none"
        verified = "verified" if is_verified(session) else "unverified"
        print(
            f"- {_display(session.session_id[:8])}  [{_display(session.status)}]  "
            f"{_display(session.title)}"
        )
        print(f"    started:      {_display(session.timestamps.start or 'unknown')}")
        print(
            f"    commands:     {session.summary.commands_count} "
            f"({session.summary.failed_commands_count} failed)"
        )
        print(f"    notes:        {session.summary.notes_count}")
        print(f"    verification: {verified}")
        print(f"    reports:      {modes_label}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()
    resolved, matches = resolve_session_id(paths, args.session_id)

    if resolved is None:
        if not matches:
            eprint(
                f"No session found matching id {args.session_id!r}. "
                "Use 'debugbrief list' to see available sessions."
            )
        else:
            eprint(
                f"Ambiguous session id {args.session_id!r} matches "
                f"{len(matches)} sessions:"
            )
            for sid in matches:
                eprint(f"  {sid}")
            eprint("Provide more characters to disambiguate.")
        return 1

    manager = SessionManager(paths)
    try:
        session = manager.load_session_file(resolved)
    except SessionError as exc:
        eprint(f"error: {exc}")
        return 1
    normalize_session_counts(session)

    if args.json:
        try:
            print(json.dumps(_json_session_payload(session), indent=2, allow_nan=False))
        except (TypeError, ValueError):
            eprint("Session contains data that cannot be represented as JSON.")
            return 1
        return 0

    ctx = build_context(session)
    modes = report_modes_for(paths, session.session_id)

    print(f"Session: {_display(session.title)}")
    print(f"  id:        {_display(session.session_id)}")
    print(f"  status:    {_display(session.status)}")
    print(f"  root:      {_display(session.project_root)}")
    print(f"  started:   {_display(session.timestamps.start or 'unknown')}")
    print(f"  ended:     {_display(session.timestamps.end or '(not ended)')}")
    if session.git.is_repo:
        branch = session.git.branch or (
            "(detached HEAD)" if session.git.detached_head else "(no branch)"
        )
        print(f"  git:       {_display(branch)}")
    else:
        print("  git:       not a Git repository")

    print("")
    print(f"Notes ({len(ctx.notes)}):")
    if ctx.notes:
        for _ts, text in ctx.notes:
            print(f"  - {_display(text)}")
    else:
        print("  (none)")

    print("")
    print(f"Relevant commands ({len(ctx.report_commands)}):")
    if ctx.report_commands:
        for rc in ctx.report_commands:
            repeat = f" x{rc.count}" if rc.count > 1 else ""
            exit_repr = "n/a" if rc.exit_code is None else str(rc.exit_code)
            print(
                f"  - {_display(rc.command)}{repeat} -> {_display(rc.status)} "
                f"(exit {exit_repr})"
            )
    else:
        print("  (none)")

    if ctx.failed_commands:
        print("")
        print(f"Failed commands ({len(ctx.failed_commands)}):")
        for rc in ctx.failed_commands:
            print(f"  - {_display(rc.command)}")

    print("")
    print(f"Verification commands ({len(ctx.verification_commands)}):")
    if ctx.verification_commands:
        for rc in ctx.verification_commands:
            print(f"  - {_display(rc.command)}")
    else:
        print("  (none passed)")

    print("")
    if session.git.is_repo and session.summary.file_changes:
        print(f"Changed files ({len(session.summary.file_changes)}):")
        for fc in session.summary.file_changes:
            print(f"  - {_display(fc.status)} {_display(fc.path)}")
    elif session.git.is_repo and session.summary.modified_files:
        print(f"Changed files ({len(session.summary.modified_files)}):")
        for path in session.summary.modified_files:
            print(f"  - {_display(path)}")
    else:
        print("Changed files: (none recorded)")

    print("")
    if modes:
        print("Reports:")
        for mode in modes:
            report = paths.existing_report_file(session.session_id, mode)
            if report is not None:
                print(f"  - {mode}: {_display(report)}")
    else:
        print("Reports: (none generated)")
    return 0


def _reconstruct_command(parts: List[str]) -> str:
    """Reconstruct the command string from the raw ``run`` tokens.

    A leading ``--`` separator is dropped. A single remaining token (the quoted
    form) is preserved verbatim. Multiple tokens (the ``--`` passthrough form)
    are joined with ``shlex.join`` so arguments containing spaces or quotes
    survive intact into storage, reports, and re-runs.
    """
    tokens = list(parts)
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return shlex.join(tokens)


def _plain_command_text(parts: List[str]) -> str:
    """Space-join the raw ``run`` tokens (minus a leading ``--``) for display.

    Used only as the auto-start title seed, where the shlex-quoted
    reconstruction would read as nested quote noise. The executed and stored
    command always comes from :func:`_reconstruct_command`.
    """
    tokens = list(parts)
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]
    if not tokens:
        return ""
    executable = Path(tokens[0]).name
    if len(tokens) > 1 and tokens[1] in {"-c", "-e", "-r"}:
        return f"{executable} {tokens[1]} <code>"
    if len(tokens) > 2 and tokens[1] == "-m":
        return f"{executable} -m {tokens[2]}"
    return " ".join([executable, *tokens[1:]])


def _silence_stdout() -> None:
    """Point stdout at devnull so a later flush on a broken pipe cannot raise.

    Used when the consumer of our stdout has closed the pipe, so neither the
    explicit flush below nor the interpreter's implicit flush at exit raises a
    second error (which CPython would otherwise turn into exit code 120).
    """
    with contextlib.suppress(OSError, ValueError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


@contextlib.contextmanager
def _deferred_sigint():
    """Block SIGINT for the duration of a small atomic critical section.

    Persisting a recorded command rewrites the session file via an atomic
    replace. If a Ctrl-C lands in the middle of that write the event can be lost,
    so SIGINT is blocked while the write happens and delivered (as the usual
    KeyboardInterrupt) immediately afterward. Only effective on the main thread;
    a no-op elsewhere.
    """
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _apply_config_defaults(args: argparse.Namespace) -> None:
    """Fill flags the user did not pass from .debugbrief.toml, then built-ins.

    Only timeout, mode, and detail have project-config defaults. The sensitive
    full-path option is deliberately CLI-only, so repository configuration cannot
    enable it. The CLI defaults PR reports to compact detail. An explicit flag
    therefore always beats config, and config always beats the built-in default.
    """
    resolvable = ("timeout", "mode", "detail", "include_full_path")
    if not any(getattr(args, name, "unset") is None for name in resolvable):
        return
    config = load_config(resolve_project_paths().project_root)
    if getattr(args, "timeout", "unset") is None:
        args.timeout = config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if getattr(args, "mode", "unset") is None:
        args.mode = config.get("default_mode", "pr")
    if getattr(args, "detail", "unset") is None:
        args.detail = config.get("detail", "compact")
    if getattr(args, "include_full_path", "unset") is None:
        args.include_full_path = False


def main(argv: Optional[List[str]] = None) -> int:
    if not is_supported_platform():
        eprint(
            "DebugBrief v1 supports Unix-like systems only (Linux, macOS, BSD).\n"
            "Windows and PowerShell are not supported in this version."
        )
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)

    # The run subparser reuses the "command" attribute for its token list, so
    # the presence of a handler is what marks a subcommand as selected.
    if not hasattr(args, "func"):
        parser.print_help()
        return 2

    _apply_config_defaults(args)

    try:
        code = args.func(args)
    except (SessionError, UnsafeStateDirectory) as exc:
        eprint(f"error: {exc}")
        return 1
    except BrokenPipeError:
        # The consumer of our stdout closed the pipe early, e.g.
        # `debugbrief list | head -1`. Return the Unix convention for SIGPIPE
        # (128 + 13) and silence stdout so exit does not raise again.
        _silence_stdout()
        return 141
    except (OSError, ValueError, RecursionError) as exc:
        eprint(f"error: {exc}")
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        eprint("Interrupted.")
        return 130

    # Flush now so a broken downstream pipe surfaces here, where it can be turned
    # into a clean 141, rather than during the interpreter's shutdown flush
    # (which CPython reports as 120 with an "Exception ignored" message).
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        _silence_stdout()
        return 141 if code == 0 else code
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
