"""Report rendering: dispatch a finalized session to a mode-specific reporter."""

from __future__ import annotations

from typing import Dict, Type

from ..models import Session
from .base import BaseReporter, build_context
from .handoff import HandoffReporter
from .incident import IncidentReporter
from .pr import PRReporter

VALID_MODES = ("pr", "handoff", "incident")

_REPORTERS: Dict[str, Type[BaseReporter]] = {
    "pr": PRReporter,
    "handoff": HandoffReporter,
    "incident": IncidentReporter,
}


def render_report(session: Session, mode: str) -> str:
    """Render a markdown report for ``session`` in the requested ``mode``."""
    if mode not in _REPORTERS:
        raise ValueError(
            f"Unknown report mode {mode!r}. Valid modes: {', '.join(VALID_MODES)}."
        )
    context = build_context(session)
    reporter = _REPORTERS[mode](context)
    return reporter.render()


__all__ = ["render_report", "VALID_MODES", "build_context"]
