"""Cross-check website fixtures and navigation against actual product output."""

from __future__ import annotations

import json
import runpy
import shlex
from html.parser import HTMLParser
from pathlib import Path

from debugbrief.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]


def test_local_links_assets_and_accessible_references():
    checker = runpy.run_path(str(ROOT / "scripts" / "check_site.py"))
    assert checker["check_site"](ROOT / "site") == []


def test_report_fixtures_are_deterministic_python_reporter_output():
    generator = runpy.run_path(str(ROOT / "scripts" / "generate_samples.py"))
    for name, expected in generator["outputs"]().items():
        assert (ROOT / "site" / name).read_text(encoding="utf-8") == expected, name


def test_incident_fixture_preserves_later_failure_and_partial_capture():
    payload = json.loads((ROOT / "site/data/sample-incident.json").read_text())
    assert payload["evidence"]["state"] == "unverified"
    assert payload["red_to_green"] is not None
    assert payload["currently_failing"][0]["status"] == "failed"
    assert payload["command_capture_status"] == "partial"


def test_documented_cli_examples_are_supported_by_the_parser():
    parser = build_parser()
    for command in [
        ["run", "--", "python", "-m", "pytest", "-q"],
        ["run", "--timeout", "900", "--", "pytest", "-q"],
        ["run", "--verify", "--", "./check.sh"],
        ["redo"],
        ["redo", "--dry-run"],
        ["note", "observed boundary"],
        ["end", "--mode", "pr", "--format", "all"],
        ["end", "--format", "html", "--open"],
        ["end", "--format", "json"],
        ["preview", "--format", "json"],
        ["open", "--html"],
        ["last", "--open"],
        ["doctor", "--fix"],
        ["recover"],
        ["status"],
    ]:
        assert parser.parse_args(command).func is not None


def test_docs_copy_buttons_use_actual_supported_commands():
    parser = build_parser()
    commands = []

    class Examples(HTMLParser):
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            for line in values.get("data-copy", "").splitlines():
                if line.startswith("debugbrief "):
                    commands.append(shlex.split(line.split(" | ")[0].split(" > ")[0])[1:])

    Examples().feed((ROOT / "site/docs.html").read_text())
    assert len(commands) >= 15
    for command in commands:
        assert parser.parse_args(command).func is not None, command


def test_current_repository_links_in_site_and_metadata():
    for path in [ROOT / "pyproject.toml", *(ROOT / "site").glob("*.html")]:
        assert "github.com/harihkk/" not in path.read_text(), path


def test_link_checker_detects_missing_files_and_fragments(tmp_path):
    checker = runpy.run_path(str(ROOT / "scripts/check_site.py"))
    (tmp_path / "index.html").write_text('<a href="absent.html">bad</a><a href="#absent">bad</a>')
    errors = checker["check_site"](tmp_path)
    assert len(errors) == 2
    assert any("missing file" in error for error in errors)
    assert any("missing fragment" in error for error in errors)
