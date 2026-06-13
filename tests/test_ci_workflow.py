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
    # Python matrix versions: the floor (3.9) through the latest advertised (3.14).
    for version in ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]:
        assert version in text
    # Never a Windows runner (Unix-only by design).
    assert "windows-latest" not in text.lower()
    # Core steps
    assert "actions/checkout@" in text
    assert "actions/setup-python@" in text
    assert "python -m pytest" in text
    assert "python -m build" in text
    # Lint and type checks run in CI.
    assert "ruff check" in text
    assert "mypy src/debugbrief" in text
    # Packaging verification: clean wheel install and pip check.
    assert "dist/*.whl" in text
    assert "pip check" in text
    # Smoke checks exercise the CLI from the installed wheel.
    for fragment in ['"$db" start', '"$db" run --', '"$db" preview', '"$db" end', '"$db" cancel']:
        assert fragment in text
