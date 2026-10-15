"""Tests for the `last` and `open` commands and the reports index."""

from __future__ import annotations

import argparse
import json
import os
import stat

import pytest

from debugbrief import cli, reports_index
from debugbrief.paths import ProjectPaths
from debugbrief.session_manager import SessionManager


@pytest.fixture
def paths(tmp_path):
    return ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)


@pytest.fixture(autouse=True)
def _patch_resolve(monkeypatch, paths):
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)
    return paths


def _make_report(paths, name, contents, mtime=None):
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    report = paths.reports_dir / name
    report.write_text(contents, encoding="utf-8")
    if mtime is not None:
        os.utime(report, (mtime, mtime))
    return report


# reports_index ------------------------------------------------------------
def test_latest_report_none(paths):
    assert reports_index.latest_report(paths.reports_dir) is None


def test_latest_report_picks_newest(paths):
    _make_report(paths, "a-pr.md", "# Old\n", mtime=1000)
    newer = _make_report(paths, "b-handoff.md", "# New\n", mtime=2000)
    assert reports_index.latest_report(paths.reports_dir) == newer


def test_infer_mode():
    from pathlib import Path

    assert reports_index.infer_mode(Path("x-pr.md")) == "pr"
    assert reports_index.infer_mode(Path("x-handoff.md")) == "handoff"
    assert reports_index.infer_mode(Path("x-incident.md")) == "incident"
    assert reports_index.infer_mode(Path("x-unknown.md")) is None


def test_first_title(paths):
    report = _make_report(paths, "x-pr.md", "# My Title\n\nbody\n")
    assert reports_index.first_title(report) == "My Title"


# last command -------------------------------------------------------------
def test_last_no_reports(paths, capsys):
    rc = cli.cmd_last(argparse.Namespace())
    assert rc == 1
    err = capsys.readouterr().err
    assert "No DebugBrief reports found" in err


def test_last_with_multiple_reports(paths, capsys):
    _make_report(paths, "a-pr.md", "# First\n", mtime=1000)
    _make_report(paths, "b-incident.md", "# Second incident\n", mtime=5000)
    rc = cli.cmd_last(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "b-incident.md" in out
    assert "incident" in out
    assert "Second incident" in out


def test_last_keeps_json_title_plain_and_unescaped(paths, capsys):
    _make_report(
        paths,
        "a-pr.json",
        json.dumps({"title": "A * title <tag>"}),
        mtime=1000,
    )
    assert cli.cmd_last(argparse.Namespace()) == 0
    assert "A * title <tag>" in capsys.readouterr().out


def test_last_handles_unreadable_report_without_traceback(paths, capsys):
    report = paths.reports_dir / "a-pr.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_bytes(b"\xff\xfe not utf8")
    assert cli.cmd_last(argparse.Namespace()) == 0
    output = capsys.readouterr().out
    assert "a-pr.md" in output
    assert "no title line found" in output


# open command -------------------------------------------------------------
def test_open_no_editor(paths, monkeypatch, capsys):
    monkeypatch.delenv("EDITOR", raising=False)
    _make_report(paths, "a-pr.md", "# Title\n", mtime=1000)
    rc = cli.cmd_open(argparse.Namespace(last=False, path=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Report path:" in out
    assert "a-pr.md" in out


def test_open_no_reports(paths, monkeypatch, capsys):
    monkeypatch.delenv("EDITOR", raising=False)
    rc = cli.cmd_open(argparse.Namespace(last=True, path=None))
    assert rc == 1
    assert "No DebugBrief reports found" in capsys.readouterr().err


def test_end_open_html_uses_the_system_browser(paths, monkeypatch):
    manager = SessionManager(paths)
    manager.start("open on end")
    manager.add_note("a finding")
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda uri: opened.append(uri) or True)
    args = argparse.Namespace(
        mode="pr",
        report_format="html",
        to_stdout=False,
        detail="compact",
        include_full_path=False,
        open=True,
    )
    assert cli.cmd_end(args) == 0
    assert opened and opened[0].endswith("-pr.html")


def test_end_stdout_open_keeps_browser_status_off_stdout(paths, monkeypatch, capsys):
    manager = SessionManager(paths)
    manager.start("stdout open")
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda uri: opened.append(uri) or True)
    args = argparse.Namespace(
        mode="pr",
        report_format="html",
        to_stdout=True,
        detail="compact",
        include_full_path=False,
        open=True,
    )
    assert cli.cmd_end(args) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("<!doctype html>")
    assert "Opening report:" not in captured.out
    assert "Opening report:" in captured.err
    assert opened


def test_open_html_uses_the_system_browser(paths, monkeypatch):
    report = _make_report(paths, "a-pr.html", "<h1>Proof</h1>", mtime=1000)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda uri: opened.append(uri) or True)
    rc = cli.cmd_open(argparse.Namespace(last=True, path=None, html=True))
    assert rc == 0
    assert opened == [report.resolve().as_uri()]


def test_open_with_fake_editor(paths, tmp_path, monkeypatch):
    marker = tmp_path / "opened.txt"
    editor = tmp_path / "fake_editor.sh"
    editor.write_text(
        f'#!/bin/sh\necho "$1" > "{marker}"\n', encoding="utf-8"
    )
    editor.chmod(editor.stat().st_mode | stat.S_IEXEC)

    report = _make_report(paths, "a-pr.md", "# Title\n", mtime=1000)
    monkeypatch.setenv("EDITOR", str(editor))
    rc = cli.cmd_open(argparse.Namespace(last=True, path=None))
    assert rc == 0
    assert marker.read_text(encoding="utf-8").strip() == str(report)


def test_open_explicit_path(paths, tmp_path, monkeypatch):
    marker = tmp_path / "opened.txt"
    editor = tmp_path / "fake_editor.sh"
    editor.write_text(
        f'#!/bin/sh\necho "$1" > "{marker}"\n', encoding="utf-8"
    )
    editor.chmod(editor.stat().st_mode | stat.S_IEXEC)

    other = _make_report(paths, "specific-handoff.md", "# Specific\n", mtime=1000)
    _make_report(paths, "newer-pr.md", "# Newer\n", mtime=9000)
    monkeypatch.setenv("EDITOR", str(editor))
    rc = cli.cmd_open(argparse.Namespace(last=False, path=str(other)))
    assert rc == 0
    assert marker.read_text(encoding="utf-8").strip() == str(other)


def test_open_rejects_symlink_explicit_path(paths, tmp_path, monkeypatch, capsys):
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    link = paths.project_root / "linked-report.md"
    link.symlink_to(outside)
    monkeypatch.setenv("EDITOR", "true")
    rc = cli.cmd_open(argparse.Namespace(last=False, path=str(link)))
    assert rc == 1
    assert "not a regular file" in capsys.readouterr().err


def test_open_missing_explicit_path(paths, monkeypatch, capsys):
    monkeypatch.setenv("EDITOR", "true")
    rc = cli.cmd_open(
        argparse.Namespace(last=False, path=str(paths.project_root / "nope.md"))
    )
    assert rc == 1
    assert "Report not found" in capsys.readouterr().err
