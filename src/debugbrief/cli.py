"""Command-line interface for DebugBrief (argparse only).

Commands:
    debugbrief start "<title>"
    debugbrief note  "<text>"
    debugbrief run   "<command>" [--shell] [--timeout N] [--no-redact]
    debugbrief end   --mode pr|handoff|incident [--format md|json|both]
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
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import __version__
from .command_runner import DEFAULT_TIMEOUT_SECONDS, run_command
from .doctor import run_doctor
from .models import COMMAND_STATUS_PASSED
from .paths import ensure_local_ignore, resolve_project_paths
from .reporters import VALID_MODES, build_context
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
    p_note.add_argument("text", help="The note text.")
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
        "command",
        nargs="+",
        help='The command to run, e.g. "python -m pytest". Quote it.',
    )
    p_run.set_defaults(func=cmd_run)

    # end ----------------------------------------------------------------
    p_end = subparsers.add_parser(
        "end", help="Finalize the session and write a markdown report."
    )
    p_end.add_argument(
        "--mode",
        required=True,
        choices=VALID_MODES,
        help="Report style to generate.",
    )
    p_end.add_argument(
        "--format",
        dest="report_format",
        choices=["md", "json", "both"],
        default="md",
        help="Report output format (default md). 'both' writes markdown and JSON.",
    )
    p_end.set_defaults(func=cmd_end)

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
    print(f"Auto-started a DebugBrief session (none was active): {session.title}")
    print(f"  id: {session.session_id}")
    if changed:
        print("  ignore: added .debugbrief/ to .git/info/exclude")
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
    print('  debugbrief run  "<command>"')
    print("  debugbrief end  --mode pr|handoff|incident")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    paths = resolve_project_paths()
    manager = SessionManager(paths)
    _ensure_session(manager, paths, args.text)
    session = manager.add_note(args.text)
    print(f"Noted ({session.summary.notes_count} total).")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    command_str = _reconstruct_command(args.command)
    paths = resolve_project_paths()
    manager = SessionManager(paths)

    if args.timeout <= 0:
        eprint("--timeout must be a positive number of seconds.")
        return 2

    # Auto-start a session if none is active so the run is never dropped.
    _ensure_session(manager, paths, command_str)

    result = run_command(
        command=command_str,
        cwd=manager.paths.project_root,
        use_shell=args.shell,
        timeout_seconds=args.timeout,
        redact=not args.no_redact,
    )
    manager.record_command(result)

    data = result.command_data
    print(f"$ {data.command}")
    if result.errored:
        eprint(f"  error:     {result.error_message}")
    elif result.timed_out:
        eprint(f"  status:    timed out after {args.timeout}s (recorded)")
    else:
        verdict = "passed" if data.classification.status == COMMAND_STATUS_PASSED else "failed"
        print(f"  status:    {verdict} (exit {data.exit_code})")
    print(f"  duration:  {data.duration_seconds}s")
    if data.classification.is_test:
        print(f"  test:      {data.classification.tool or 'unknown'}")
    if data.stdout_truncated:
        print("  note:      stdout preview was truncated")
    if data.stderr_truncated:
        print("  note:      stderr preview was truncated")
    if data.redacted:
        print("  note:      secret-like values were redacted")
    return result.propagated_exit_code


def cmd_end(args: argparse.Namespace) -> int:
    manager = _manager()
    session = manager.end(args.mode, args.report_format)
    print(f"Session completed: {session.title}")
    print(f"  mode:      {args.mode}")
    if args.report_format in ("md", "both"):
        print(
            f"  report:    {manager.paths.report_file(session.session_id, args.mode)}"
        )
    if args.report_format in ("json", "both"):
        print(
            f"  json:      "
            f"{manager.paths.report_json_file(session.session_id, args.mode)}"
        )
    print(f"  session:   {manager.paths.session_file(session.session_id)}")
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
    """Reconstruct the command string from parsed positional tokens.

    A single quoted argument (the documented usage) is preserved verbatim.
    Multiple tokens are joined with single spaces as a best-effort fallback.
    """
    if len(parts) == 1:
        return parts[0]
    return " ".join(parts)


def main(argv: Optional[List[str]] = None) -> int:
    if not is_supported_platform():
        eprint(
            "DebugBrief v1 supports Unix-like systems only (Linux, macOS, BSD).\n"
            "Windows and PowerShell are not supported in this version."
        )
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    try:
        return args.func(args)
    except SessionError as exc:
        eprint(f"error: {exc}")
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        eprint("Interrupted.")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
