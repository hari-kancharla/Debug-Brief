"""Report rendering: dispatch a finalized session to a mode-specific reporter."""

from __future__ import annotations

from typing import Any, Dict, Type

from ..models import Session
from .base import BaseReporter, build_context

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


def render_report(session: Session, mode: str, detail: str = "full") -> str:
    """Render a markdown report for ``session`` in the requested ``mode``.

    ``detail`` is ``"full"`` (default) or ``"compact"``. Compact only affects the
    PR report, which keeps the scannable parts visible and folds the metadata and
    timeline into a collapsible section; other modes ignore it.
    """
    _check_mode(mode)
    context = build_context(session)
    reporter = _reporters()[mode](context)
    reporter.detail = detail
    return reporter.render()


def render_report_json(session: Session, mode: str) -> Dict[str, Any]:
    """Render the same derived content as a structured JSON-ready dict.

    The keys mirror the markdown report's derived sections so the two formats
    stay in sync. Like the markdown, fields with no evidence are null/empty.
    """
    _check_mode(mode)
    context = build_context(session)
    d = context.derivation
    s = session

    rtg = None
    if d.red_to_green is not None:
        rtg = {
            "command": d.red_to_green.command,
            "failed_at": d.red_to_green.failed_at,
            "passed_at": d.red_to_green.passed_at,
            "window_seconds": d.red_to_green.window_seconds,
            "changed_files": list(d.red_to_green.changed_files),
        }

    return {
        "mode": mode,
        "session_id": s.session_id,
        "title": s.title,
        "status": s.status,
        "project_root": s.project_root,
        "started_at": s.timestamps.start,
        "ended_at": s.timestamps.end,
        "git": {
            "is_repo": s.git.is_repo,
            "branch": s.git.branch,
            "detached_head": s.git.detached_head,
            "initial_sha": s.git.initial_sha,
            "final_sha": s.git.final_sha,
        },
        "counts": {
            "notes": s.summary.notes_count,
            "commands": s.summary.commands_count,
            "failed_commands": s.summary.failed_commands_count,
        },
        "one_liner": d.one_liner,
        "reproduce_command": d.reproduce_command,
        "verify_command": d.verify_command,
        "red_to_green": rtg,
        "observed_error": d.observed_error,
        "ruled_out": [
            {
                "command": r.command,
                "status": r.status,
                "exit_code": r.exit_code,
                "timestamp": r.timestamp,
            }
            for r in d.ruled_out
        ],
        "timeline": [
            {"timestamp": e.timestamp, "kind": e.kind, "text": e.text}
            for e in context.timeline
        ],
        "changed_files": [
            {"status": fc.status, "path": fc.path} for fc in s.summary.file_changes
        ],
        "verification": [
            {"command": rc.command, "tool": rc.tool, "is_test": rc.is_test}
            for rc in context.verification_commands
        ],
        "notes": [text for _ts, text in context.notes],
        "redaction_applied": d.redaction_applied,
    }


__all__ = ["render_report", "render_report_json", "VALID_MODES", "build_context"]
