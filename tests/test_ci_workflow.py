"""Sanity checks for the CI workflow file (no YAML dependency required)."""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_exists():
    assert WORKFLOW.is_file()


def test_ci_workflow_basic_structure():
    text = WORKFLOW.read_text(encoding="utf-8")
    # Triggers
    assert "pull_request" in text
    assert "branches: [main]" in text
    # Python matrix versions
    for version in ["3.10", "3.11", "3.12"]:
        assert version in text
    # Core steps
    assert "actions/checkout@" in text
    assert "actions/setup-python@" in text
    assert "python -m pytest" in text
    assert "python -m build" in text
    # Smoke checks
    for command in ["debugbrief --help", "debugbrief doctor", "debugbrief start", "debugbrief end --mode pr", "debugbrief last"]:
        assert command in text
