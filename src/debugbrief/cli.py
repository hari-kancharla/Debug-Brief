"""Command-line interface for DebugBrief (argparse only).

Commands:
    debugbrief start "<title>"
    debugbrief note  <text ...>
    debugbrief run   [--shell] [--timeout N] [--no-redact] -- <command ...>
    debugbrief run   "<command>"
    debugbrief redo  [--timeout N] [--no-redact] [--verify]
    debugbrief preview [--mode pr|handoff|incident]
    debugbrief end   [--mode pr|handoff|incident] [--format md|json|both] [--stdout]
    debugbrief cancel [--yes]
    debugbrief status
    debugbrief doctor [--fix]
    debugbrief last
    debugbrief open  [--last | --path PATH]
    debugbrief list  [--json]
    debugbrief show  <session_id> [--json]

run and note auto-start a session if none is active.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import __version__
from .command_runner import DEFAULT_TIMEOUT_SECONDS, RunResult, run_command
from .doctor import FAIL, run_doctor
from .models import COMMAND_STATUS_PASSED, CommandData
from .paths import ensure_local_ignore, resolve_project_paths
from .redaction import PLACEHOLDER
from .reporters import VALID_MODES, build_context, render_report
from .reports_index import first_title, infer_mode, latest_report
from .session_manager import SessionError, SessionManager
from .sessions_index import (
    is_verified,
    load_all_sessions,
    report_modes_for,
    resolve_session_id,
)
from .utils import eprint, is_supported_platform

PROG = "debugbrief"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Record the meaningful context of a debugging session and turn it "
            "into a useful markdown brief (PR, handoff, or incident)."
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
    p_start.add_argument(
        "--shell",
        action="store_true",
        help=(
            "EXPERIMENTAL: spawn an interactive subshell to capture shell "
            "history. Not available in v1 (see README)."
        ),
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
            "Run the command through the system shell (enables pipes, "
            "redirection, &&). Default parses with shlex and runs without a shell."
        ),
    )
    p_run.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
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
        default=DEFAULT_TIMEOUT_SECONDS,
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
    p_redo.set_defaults(func=cmd_redo)

    # preview ------------------------------------------------------------
    p_preview = subparsers.add_parser(
        "preview",
        help="Print the report for the active session without ending it.",
    )
    p_preview.add_argument(
        "--mode",
        default="pr",
        choices=VALID_MODES,
        help="Report style to preview (default pr).",
    )
    p_preview.set_defaults(func=cmd_preview)

    # end ----------------------------------------------------------------
    p_end = subparsers.add_parser(
        "end", help="Finalize the session and write a markdown report."
    )
    p_end.add_argument(
        "--mode",
        default="pr",
        choices=VALID_MODES,
        help="Report style to generate (default pr).",
    )
    p_end.add_argument(
        "--format",
        dest="report_format",
        choices=["md", "json", "both"],
        default="md",
        help="Report output format (default md). 'both' writes markdown and JSON.",
    )
    p_end.add_argument(
        "--stdout",
        dest="to_stdout",
        action="store_true",
        help=(
            "Print the rendered markdown report to stdout (the file is still "
            "written). Informational lines move to stderr, so the output pipes "
            "cleanly: debugbrief end --stdout | gh pr comment --body-file -"
        ),
    )
    p_end.set_defaults(func=cmd_end)

    # cancel -------------------------------------------------------------
    p_cancel = subparsers.add_parser(
        "cancel",
        help="Discard the active session without writing a report.",
    )
    p_cancel.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    p_cancel.set_defaults(func=cmd_cancel)

    # status -------------------------------------------------------------
    p_status = subparsers.add_parser("status", help="Show the active session status.")
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
    p_last.set_defaults(func=cmd_last)

    # open ---------------------------------------------------------------
    p_open = subparsers.add_parser(
        "open", help="Open a report in $EDITOR (defaults to the latest report)."
    )
    p_open.add_argument(
        "--last", action="store_true", help="Open the latest report (default)."
    )
    p_open.add_argument(
        "--path", metavar="REPORT", help="Open a specific report file."
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
    eprint(f"Auto-started a DebugBrief session (none was active): {session.title}")
    eprint(f"  id: {session.session_id}")
    if changed:
        eprint("  ignore: added .debugbrief/ to .git/info/exclude")
    for warning in warnings:
        eprint(f"  warning: {warning}")
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
    print(f"  id:        {session.session_id}")
    print(f"  title:     {session.title}")
    print(f"  root:      {session.project_root}")
    if session.git.is_repo:
        branch = session.git.branch or (
            "(detached HEAD)" if session.git.detached_head else "(no branch)"
        )
        print(f"  git:       repo on {branch}")
    else:
        print("  git:       not a Git repository (continuing locally)")
    if changed:
        print("  ignore:    added .debugbrief/ to .git/info/exclude")
    for warning in warnings:
        eprint(f"  warning:   {warning}")
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
    print(f"Noted ({session.summary.notes_count} total).")
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
    eprint(f"$ {command_str}")
    result = run_command(
        command=command_str,
        cwd=invocation_cwd,
        use_shell=args.shell,
        timeout_seconds=args.timeout,
        redact=not args.no_redact,
        force_verification=args.verify,
    )
    result.command_data.invocation_cwd = str(invocation_cwd)
    with _deferred_sigint():
        manager.record_command(result)
    _print_command_outcome(result, args.timeout)
    return result.propagated_exit_code


def _print_command_outcome(result: RunResult, timeout_seconds: int) -> None:
    """Report a captured command's outcome on stderr (shared by run and redo)."""
    data = result.command_data
    if result.errored:
        eprint(f"  error:     {result.error_message}")
    elif result.interrupted:
        eprint("  status:    interrupted (recorded)")
    elif result.broken_pipe:
        eprint("  status:    stopped, downstream pipe closed (recorded)")
    elif result.timed_out:
        eprint(f"  status:    timed out after {timeout_seconds}s (recorded)")
    else:
        verdict = "passed" if data.classification.status == COMMAND_STATUS_PASSED else "failed"
        eprint(f"  status:    {verdict} (exit {data.exit_code})")
    eprint(f"  duration:  {data.duration_seconds}s")
    if data.classification.is_test:
        eprint(f"  test:      {data.classification.tool or 'unknown'}")
    if data.stdout_truncated:
        eprint("  note:      stdout preview was truncated")
    if data.stderr_truncated:
        eprint("  note:      stderr preview was truncated")
    if data.redacted:
        eprint("  note:      secret-like values were redacted")
    if result.warning:
        eprint(f"  warning:   {result.warning}")


def cmd_redo(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()
    manager = SessionManager(paths)

    if args.timeout <= 0:
        eprint("--timeout must be a positive number of seconds.")
        return 2

    session = manager.load_active()
    if session is None:
        eprint(
            "No active DebugBrief session, so there is nothing to redo. "
            "Run a command first: debugbrief run -- <command>"
        )
        return 1

    command_events = session.command_events()
    if not command_events:
        eprint(
            "No commands have been captured in this session yet. "
            "Run one first: debugbrief run -- <command>"
        )
        return 1

    last = CommandData.from_dict(command_events[-1].data)
    if PLACEHOLDER in last.command:
        eprint(
            f"The last stored command contains {PLACEHOLDER}, a redaction "
            "placeholder, not the real text, so it cannot be re-run. "
            "Run the command again yourself: debugbrief run -- <command>"
        )
        return 1

    # A redo of a command originally declared with --verify stays a declared
    # check without retyping the flag; an explicit --verify also works.
    inherit_verify = last.classification.tool == "custom"

    invocation_cwd = Path.cwd()
    eprint(f"$ {last.command}  (redo)")
    result = run_command(
        command=last.command,
        cwd=invocation_cwd,
        use_shell=last.used_shell,
        timeout_seconds=args.timeout,
        redact=not args.no_redact,
        force_verification=args.verify or inherit_verify,
    )
    result.command_data.invocation_cwd = str(invocation_cwd)
    with _deferred_sigint():
        manager.record_command(result)
    _print_command_outcome(result, args.timeout)
    return result.propagated_exit_code


def cmd_end(args: argparse.Namespace) -> int:
    manager = _manager()
    session = manager.end(args.mode, args.report_format)
    # With --stdout the report itself owns stdout; everything informational
    # moves to stderr so the output pipes cleanly.
    info = eprint if args.to_stdout else print
    info(f"Session completed: {session.title}")
    info(f"  mode:      {args.mode}")
    if args.report_format in ("md", "both"):
        info(
            f"  report:    {manager.paths.report_file(session.session_id, args.mode)}"
        )
    if args.report_format in ("json", "both"):
        info(
            f"  json:      "
            f"{manager.paths.report_json_file(session.session_id, args.mode)}"
        )
    info(f"  session:   {manager.paths.session_file(session.session_id)}")
    if args.to_stdout:
        sys.stdout.write(render_report(session, args.mode))
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    manager = _manager()
    markdown = manager.preview(args.mode)
    banner = "_Preview of an active session. Run debugbrief end to finalize._"
    lines = markdown.splitlines()
    if lines and lines[0].startswith("# "):
        rendered = lines[0] + "\n\n" + banner + "\n" + "\n".join(lines[1:]) + "\n"
    else:  # pragma: no cover - reports always start with a title
        rendered = banner + "\n\n" + markdown
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
            answer = input(f"Discard active session '{session.title}'? [y/N] ")
        except EOFError:
            # No stdin to answer with (e.g. a pipe): treat as a decline.
            answer = ""
        if answer.strip().lower() != "y":
            eprint("Aborted; the session is still active.")
            return 1

    manager.cancel()
    eprint(f"Discarded session '{session.title}' (no report written).")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    manager = _manager()
    status = manager.build_status()

    if not status.get("active"):
        print("No active DebugBrief session.")
        print('Start one with: debugbrief start "<title>"')
        return 0

    if status.get("interrupted"):
        print("A session appears INTERRUPTED or inconsistent.")
        print(f"  id:        {status.get('session_id')}")
        if status.get("title"):
            print(f"  title:     {status.get('title')}")
        reason = status.get("reason")
        if reason:
            print(f"  reason:    {reason}")
        print("")
        print("Recovery:")
        print("  - Inspect .debugbrief/active_session.json and the sessions/ folder.")
        print("  - Remove .debugbrief/active_session.json to clear the active pointer,")
        print("    then start a fresh session.")
        return 0

    print(f"Active session: {status.get('title')}")
    print(f"  id:        {status.get('session_id')}")
    print(f"  status:    {status.get('status')}")
    print(f"  root:      {status.get('project_root')}")
    print(f"  started:   {status.get('start')}")
    print(f"  notes:     {status.get('notes_count')}")
    print(
        f"  commands:  {status.get('commands_count')} "
        f"({status.get('failed_commands_count')} failed)"
    )
    if status.get("is_repo"):
        branch = status.get("branch") or (
            "(detached HEAD)" if status.get("detached_head") else "(no branch)"
        )
        print(f"  git:       {branch}")
    else:
        print("  git:       not a Git repository")
    for warning in status.get("warnings", []):
        print(f"  warning:   {warning}")
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
    print(f"  project root:  {paths.project_root}")
    if paths.is_git_repo:
        print("  git:           repository detected (.debugbrief/ is ignored locally)")
    else:
        print("  git:           not a Git repository (Git sections are omitted)")
    print(f"  health:        {report.summary}")

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
    action = result["action"]
    if action == "healthy":
        print(f"Active session is healthy, nothing to recover: {result['detail']}")
    elif action == "cleared_broken_pointer":
        print("Cleared a broken active-session pointer so a new session can start.")
        print(f"  reason: {result['detail']}")
    elif action == "cleared_stale_pointer":
        print(f"Cleared a stale pointer to a {result['detail']} session.")
    else:
        print("No active-session pointer; nothing to recover.")
    corrupt = result["corrupt"]
    if corrupt:
        print("")
        print(f"Found {len(corrupt)} unreadable session file(s), left in place:")
        for name in corrupt:
            print(f"  - {name}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()
    report = run_doctor(paths, fix=args.fix)
    print("DebugBrief doctor")
    print("=================")
    for check in report.checks:
        detail = f" - {check.detail}" if check.detail else ""
        print(f"[{check.level}] {check.name}{detail}")
    print("")
    print(report.summary)
    return report.exit_code


def cmd_last(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()
    report_path = latest_report(paths.reports_dir)
    if report_path is None:
        eprint(
            "No DebugBrief reports found. Generate one with: "
            "debugbrief end --mode pr|handoff|incident"
        )
        return 1

    mode = infer_mode(report_path) or "unknown"
    title = first_title(report_path)
    mtime = datetime.fromtimestamp(report_path.stat().st_mtime)
    print(f"Latest report: {report_path}")
    print(f"  mode:      {mode}")
    print(f"  modified:  {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  title:     {title or '(no title line found)'}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()

    target: Optional[Path]
    if args.path:
        target = Path(args.path).expanduser()
        if not target.exists():
            eprint(f"Report not found: {target}")
            return 1
    else:
        target = latest_report(paths.reports_dir)
        if target is None:
            eprint(
                "No DebugBrief reports found to open. Generate one with: "
                "debugbrief end --mode pr|handoff|incident"
            )
            return 1

    editor = os.environ.get("EDITOR", "").strip()
    if not editor:
        print(f"Report path: {target}")
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
        eprint(f"Report path: {target}")
        return 1

    if completed.returncode != 0:
        eprint(f"Editor exited with code {completed.returncode}.")
        eprint(f"Report path: {target}")
        return 1
    return 0


def _session_summary_dict(paths, session) -> dict:
    modes = report_modes_for(paths, session.session_id)
    return {
        "session_id": session.session_id,
        "short_id": session.session_id[:8],
        "status": session.status,
        "title": session.title,
        "start": session.timestamps.start,
        "end": session.timestamps.end,
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
        print(f"- {session.session_id[:8]}  [{session.status}]  {session.title}")
        print(f"    started:      {session.timestamps.start or 'unknown'}")
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

    if args.json:
        print(json.dumps(session.to_dict(), indent=2))
        return 0

    ctx = build_context(session)
    modes = report_modes_for(paths, session.session_id)

    print(f"Session: {session.title}")
    print(f"  id:        {session.session_id}")
    print(f"  status:    {session.status}")
    print(f"  root:      {session.project_root}")
    print(f"  started:   {session.timestamps.start or 'unknown'}")
    print(f"  ended:     {session.timestamps.end or '(not ended)'}")
    if session.git.is_repo:
        branch = session.git.branch or (
            "(detached HEAD)" if session.git.detached_head else "(no branch)"
        )
        print(f"  git:       {branch}")
    else:
        print("  git:       not a Git repository")

    print("")
    print(f"Notes ({len(ctx.notes)}):")
    if ctx.notes:
        for _ts, text in ctx.notes:
            print(f"  - {text}")
    else:
        print("  (none)")

    print("")
    print(f"Relevant commands ({len(ctx.report_commands)}):")
    if ctx.report_commands:
        for rc in ctx.report_commands:
            repeat = f" x{rc.count}" if rc.count > 1 else ""
            exit_repr = "n/a" if rc.exit_code is None else str(rc.exit_code)
            print(f"  - {rc.command}{repeat} -> {rc.status} (exit {exit_repr})")
    else:
        print("  (none)")

    if ctx.failed_commands:
        print("")
        print(f"Failed commands ({len(ctx.failed_commands)}):")
        for rc in ctx.failed_commands:
            print(f"  - {rc.command}")

    print("")
    print(f"Verification commands ({len(ctx.verification_commands)}):")
    if ctx.verification_commands:
        for rc in ctx.verification_commands:
            print(f"  - {rc.command}")
    else:
        print("  (none passed)")

    print("")
    if session.git.is_repo and session.summary.file_changes:
        print(f"Changed files ({len(session.summary.file_changes)}):")
        for fc in session.summary.file_changes:
            print(f"  - {fc.status} {fc.path}")
    elif session.git.is_repo and session.summary.modified_files:
        print(f"Changed files ({len(session.summary.modified_files)}):")
        for path in session.summary.modified_files:
            print(f"  - {path}")
    else:
        print("Changed files: (none recorded)")

    print("")
    if modes:
        print("Reports:")
        for mode in modes:
            print(f"  - {mode}: {paths.report_file(session.session_id, mode)}")
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
    return " ".join(tokens)


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

    try:
        code = args.func(args)
    except SessionError as exc:
        eprint(f"error: {exc}")
        return 1
    except BrokenPipeError:
        # The consumer of our stdout closed the pipe early, e.g.
        # `debugbrief list | head -1`. Return the Unix convention for SIGPIPE
        # (128 + 13) and silence stdout so exit does not raise again.
        _silence_stdout()
        return 141
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
