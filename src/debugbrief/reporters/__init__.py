"""Report rendering: dispatch a finalized session to a mode-specific reporter."""

from __future__ import annotations

from typing import Any, Dict, Optional, Type

from ..markdown import (
    normalize_free_text,
    normalize_plain_text,
    normalize_text,
    plain_markdown,
)
from ..models import Session
from .base import (
    BaseReporter,
    build_context,
    report_project_root,
    report_warnings,
    report_working_directory,
)
from .html import render_report_html

VALID_MODES = ("pr", "handoff", "incident")


def _reporters() -> Dict[str, Type[BaseReporter]]:
    # Imported lazily so the mode modules can import from .base without a cycle.
    from .handoff import HandoffReporter
    from .incident import IncidentReporter
    from .pr import PRReporter

    return {"pr": PRReporter, "handoff": HandoffReporter, "incident": IncidentReporter}


def _check_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise ValueError(
            f"Unknown report mode {mode!r}. Valid modes: {', '.join(VALID_MODES)}."
        )


def _plain_summary(value: Optional[str]) -> Optional[str]:
    return normalize_text(plain_markdown(value)) if value is not None else None


def render_report(
    session: Session,
    mode: str,
    detail: str = "full",
    include_full_path: bool = False,
) -> str:
    """Render a markdown report for ``session`` in the requested ``mode``.

    ``detail`` is ``"full"`` (default) or ``"compact"``. Compact only affects the
    PR report, which keeps the scannable parts visible and folds the metadata and
    timeline into a collapsible section; other modes ignore it. Absolute project
    roots are hidden unless ``include_full_path`` is true.
    """
    _check_mode(mode)
    context = build_context(session)
    reporter = _reporters()[mode](context)
    reporter.detail = detail
    reporter.include_full_path = include_full_path
    return reporter.render()


def render_report_json(
    session: Session, mode: str, include_full_path: bool = False
) -> Dict[str, Any]:
    """Render the same derived content as a structured JSON-ready dict.

    The keys mirror the markdown report's derived sections so the two formats
    stay in sync. Like the markdown, fields with no evidence are null/empty.
    Absolute project roots are hidden unless ``include_full_path`` is true.
    """
    _check_mode(mode)
    context = build_context(session)
    d = context.derivation
    s = session

    file_changes = (
        s.summary.file_changes
        if isinstance(s.summary.file_changes, list)
        else []
    )
    changed_files = [
        {
            "status": normalize_free_text(fc.status),
            "path": normalize_plain_text(fc.path),
        }
        for fc in file_changes
    ]
    if not changed_files:
        modified_files = (
            s.summary.modified_files
            if isinstance(s.summary.modified_files, list)
            else []
        )
        changed_files = [
            {"status": "M", "path": normalize_plain_text(path)}
            for path in modified_files
        ]

    rtg = None
    if d.red_to_green is not None:
        rtg = {
            "command": normalize_plain_text(d.red_to_green.command),
            "failed_at": normalize_text(d.red_to_green.failed_at),
            "passed_at": normalize_text(d.red_to_green.passed_at),
            "window_seconds": d.red_to_green.window_seconds,
            "changed_files": [
                normalize_plain_text(path) for path in d.red_to_green.changed_files
            ],
            "commit_boundary": d.red_to_green.commit_boundary,
            "working_directory": report_working_directory(s, d.red_to_green.invocation_cwd),
        }

    return {
        "schema_version": 1,
        "mode": mode,
        "session_id": normalize_free_text(s.session_id),
        "title": normalize_free_text(s.title),
        "status": normalize_free_text(s.status),
        "project_root": normalize_text(report_project_root(s, include_full_path)),
        "warnings": [normalize_text(warning) for warning in report_warnings(s)],
        "started_at": normalize_text(s.timestamps.start)
        if s.timestamps.start is not None
        else None,
        "ended_at": normalize_text(s.timestamps.end)
        if s.timestamps.end is not None
        else None,
        "git": {
            "is_repo": bool(s.git.is_repo),
            "branch": normalize_free_text(s.git.branch)
            if s.git.branch is not None
            else None,
            "detached_head": bool(s.git.detached_head),
            "initial_sha": normalize_free_text(s.git.initial_sha)
            if s.git.initial_sha is not None
            else None,
            "final_sha": normalize_free_text(s.git.final_sha)
            if s.git.final_sha is not None
            else None,
        },
        "counts": {
            "notes": s.summary.notes_count,
            "commands": s.summary.commands_count,
            "failed_commands": s.summary.failed_commands_count,
        },
        "line_stats": {
            "added": s.summary.lines_added,
            "deleted": s.summary.lines_deleted,
        },
        "one_liner": _plain_summary(d.one_liner),
        "evidence": {
            "state": normalize_free_text(d.proof_state),
            "basis": normalize_free_text(d.proof_basis),
            "command": normalize_plain_text(d.proof_command)
            if d.proof_command
            else None,
            "scope": (
                "This verifies the recorded check, not the whole issue."
                if d.proof_state == "verified"
                else "This describes recorded checks, not the whole issue."
            ),
            "root_cause": "not_inferred",
            "invalid_verification_commands": [
                normalize_plain_text(command)
                for command in d.invalid_verification_commands
            ],
        },
        "reproduce_command": (
            normalize_plain_text(d.reproduce_command) if d.reproduce_command else None
        ),
        "verify_command": (
            normalize_plain_text(d.verify_command) if d.verify_command else None
        ),
        "reproduce_working_directory": report_working_directory(s, d.reproduce_invocation_cwd),
        "verify_working_directory": report_working_directory(s, d.verify_invocation_cwd),
        "red_to_green": rtg,
        "observed_error": normalize_text(d.observed_error) if d.observed_error else None,
        "ruled_out": [
            {
                "command": normalize_plain_text(r.command),
                "status": normalize_free_text(r.status),
                "exit_code": r.exit_code,
                "timestamp": normalize_text(r.timestamp),
                "working_directory": report_working_directory(s, r.invocation_cwd),
            }
            for r in d.ruled_out
        ],
        "timeline": [
            {
                "timestamp": normalize_text(e.timestamp),
                "kind": normalize_free_text(e.kind),
                "status": e.status,
                "working_directory": e.working_directory,
                "text": normalize_text(e.plain_text if e.plain_text is not None else e.text),
            }
            for e in context.timeline
        ],
        "changed_files": changed_files,
        "verification": [
            {
                "command": normalize_plain_text(rc.command),
                "tool": normalize_plain_text(rc.tool) if rc.tool else None,
                "is_test": bool(rc.is_test),
                "working_directory": report_working_directory(s, rc.invocation_cwd),
            }
            for rc in context.verification_commands
        ],
        # Additive structured history. Keep every attempt, including successful
        # noise commands and retries; the concise report list is deduplicated.
        # Omit absolute invocation directories and fingerprints from shareable reports.
        "commands": [
            {
                "command": normalize_plain_text(record.command),
                "timestamp": normalize_text(record.timestamp),
                "status": record.status,
                "exit_code": record.exit_code,
                "duration_seconds": record.duration_seconds,
                "is_test": record.is_test,
                "is_verification": record.is_verification,
                "tool": record.tool,
                "stdout_preview": normalize_text(record.stdout_preview),
                "stderr_preview": normalize_text(record.stderr_preview),
                "stdout_truncated": record.stdout_truncated,
                "stderr_truncated": record.stderr_truncated,
                "redacted": record.redacted,
                "working_directory": report_working_directory(s, record.invocation_cwd),
            }
            for record in d.command_records
        ],
        "currently_failing": [
            {
                "command": normalize_plain_text(record.command),
                "status": record.status,
                "exit_code": record.exit_code,
                "timestamp": normalize_text(record.last_timestamp),
                "working_directory": report_working_directory(s, record.invocation_cwd),
            }
            for record in context.currently_failing
        ],
        "notes": [normalize_text(text) for _ts, text in context.notes],
        "redaction_applied": bool(d.redaction_applied),
        "command_capture_status": normalize_free_text(
            s.summary.command_capture_status
        ),
    }


__all__ = [
    "render_report",
    "render_report_json",
    "render_report_html",
    "VALID_MODES",
    "build_context",
]
