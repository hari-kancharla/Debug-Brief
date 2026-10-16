"""Reproducible illustrative fixtures rendered by the real report implementation.

Run from the repository root: python scripts/generate_samples.py
No command is executed; these are explicitly labelled example session inputs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from debugbrief.models import (  # noqa: E402
    CommandClassification,
    CommandData,
    Event,
    FileChange,
    GitState,
    Session,
    Summary,
    Timestamps,
)
from debugbrief.reporters import render_report, render_report_html, render_report_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def sample_session(unverified=False):
    session = Session(
        session_id="11111111-2222-4333-8444-555555555555",
        title="Handle an expired cache entry",
        project_root="/example/cache-service",
        status="COMPLETED",
        timestamps=Timestamps(start="2026-01-01T12:02:00Z", end="2026-01-01T12:06:00Z"),
        git=GitState(
            is_repo=True, branch="fix/cache-expiry", initial_sha="a7c40e1", final_sha="a7c40e1"
        ),
        summary=Summary(
            modified_files=["src/cache.py", "tests/test_cache.py"],
            file_changes=[FileChange("M", "src/cache.py"), FileChange("M", "tests/test_cache.py")],
            lines_added=14,
            lines_deleted=3,
            commands_count=3,
            failed_commands_count=1,
            notes_count=2,
            command_capture_status="partial" if unverified else "full",
        ),
    )
    session.events = [Event.snapshot({"phase": "start"}, "2026-01-01T12:02:00Z")]
    for minute, command, status, tool, output in [
        (
            2,
            "python -m pytest -q tests/test_cache.py",
            "failed",
            "pytest",
            "E AssertionError: expired entry returned instead of a cache miss\n1 failed",
        ),
        (4, "python -m pytest -q tests/test_cache.py", "passed", "pytest", "4 passed"),
        (
            5,
            "python -m ruff check src tests",
            "failed" if unverified else "passed",
            "ruff",
            "E501 line too long" if unverified else "All checks passed!",
        ),
    ]:
        timestamp = f"2026-01-01T12:0{minute}:12Z"
        passed = status == "passed"
        data = CommandData(
            command=command,
            started_at=timestamp,
            ended_at=timestamp,
            duration_seconds=0.4 if tool == "pytest" else 0.08,
            exit_code=0 if passed else 1,
            stdout_preview=output,
            classification=CommandClassification(
                status=status,
                is_test=tool == "pytest",
                is_verification=passed,
                tool=tool,
            ),
            git_changed_files=[] if minute == 2 else session.summary.modified_files,
        )
        session.events.append(Event.command(data, timestamp))
    session.events.extend(
        [
            Event.note(
                "Expired entries still reach the read path. Check the expiry boundary.",
                "2026-01-01T12:03:00Z",
            ),
            Event.note(
                "Return a cache miss when the entry expires. Added the boundary case to the test.",
                "2026-01-01T12:04:00Z",
            ),
            Event.snapshot({"phase": "end"}, "2026-01-01T12:06:00Z"),
        ]
    )
    if unverified:
        session.warnings = [
            "Capture was incomplete; commands outside DebugBrief are not in this record."
        ]
        session.summary.failed_commands_count = 2
    return session


def outputs():
    result = {}
    for mode in ("pr", "handoff", "incident"):
        session = sample_session(unverified=mode == "incident")
        payload = render_report_json(session, mode)
        result[f"data/sample-{mode}.json"] = json.dumps(payload, indent=2) + "\n"
        filename = "sample-report.html" if mode == "pr" else f"sample-{mode}.html"
        result[filename] = render_report_html(session, mode)
        result[f"data/sample-{mode}.md"] = render_report(session, mode)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check fixtures without writing them")
    args = parser.parse_args()
    mismatches = []
    for name, content in outputs().items():
        target = ROOT / "site" / name
        if args.check:
            if not target.is_file() or target.read_text(encoding="utf-8") != content:
                mismatches.append(name)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    if mismatches:
        print("Stale report fixtures: " + ", ".join(mismatches), file=sys.stderr)
        sys.exit(1)
    print("Verified 9 report fixtures." if args.check else "Generated 9 report fixtures.")
