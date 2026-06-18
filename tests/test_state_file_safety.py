"""Tests that individual state files are never followed when they are symlinks
or special files, even inside a real .debugbrief/sessions or reports directory.

Reading a planted link could expose a file outside the project; a FIFO could
block. The safe-read layer rejects both, while valid sessions and reports stay
accessible.
"""

from __future__ import annotations

import os
import sys

import pytest

from debugbrief import cli
from debugbrief.paths import ProjectPaths, UnsafeStateDirectory
from debugbrief.reports_index import first_title, latest_report, list_reports
from debugbrief.session_manager import SessionError, SessionManager
from debugbrief.sessions_index import load_all_sessions
from debugbrief.utils import open_regular_binary, read_json_safe

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink/FIFO")


@pytest.fixture
def project(tmp_path):
    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    return paths, SessionManager(paths)


def _real_session_with_report(mgr):
    session = mgr.start("real session")
    mgr.end("pr")  # writes a real report and completes the session
    return session


def _plant_dangling_active_pointer(paths):
    # A dangling symlink: lexists() is True but exists() is False, so it must be
    # reported as unsafe, not silently treated as "no active session".
    paths.ensure_directories()
    paths.active_session_file.symlink_to(paths.base_dir / "missing-target")


# Active-session pointer: a dangling symlink is unsafe, not "no session" -------
def test_status_refuses_a_dangling_active_pointer(project):
    paths, mgr = project
    _plant_dangling_active_pointer(paths)
    with pytest.raises(UnsafeStateDirectory):
        mgr.build_status()


def test_load_active_refuses_a_dangling_active_pointer(project):
    paths, mgr = project
    _plant_dangling_active_pointer(paths)
    with pytest.raises(UnsafeStateDirectory):
        mgr.load_active()


def test_recover_clears_a_dangling_active_pointer(project):
    paths, mgr = project
    _plant_dangling_active_pointer(paths)
    result = mgr.recover()
    assert result["action"] == "cleared_broken_pointer"
    # The dangling symlink is actually removed (exists() would have missed it).
    assert not os.path.lexists(paths.active_session_file)


# Sessions --------------------------------------------------------------------
def test_load_all_sessions_skips_symlinked_session_keeping_valid_ones(project, tmp_path):
    paths, mgr = project
    real = _real_session_with_report(mgr)
    external = tmp_path / "outside.json"
    external.write_text(
        '{"session_id":"evil","title":"EVIL","project_root":"/x","events":[]}',
        encoding="utf-8",
    )
    (paths.sessions_dir / "evil.json").symlink_to(external)

    ids = {s.session_id for s in load_all_sessions(paths)}
    assert real.session_id in ids  # the valid session is still listed
    assert "evil" not in ids  # the symlink was skipped, not followed


def test_load_all_sessions_skips_symlink_to_arbitrary_text(project, tmp_path):
    paths, mgr = project
    _real_session_with_report(mgr)
    junk = tmp_path / "notjson.txt"
    junk.write_text("not json at all", encoding="utf-8")
    (paths.sessions_dir / "x.json").symlink_to(junk)
    assert len(load_all_sessions(paths)) == 1  # skipped, no crash on the junk


def test_load_all_sessions_skips_fifo_without_blocking(project):
    paths, mgr = project
    _real_session_with_report(mgr)
    os.mkfifo(paths.sessions_dir / "pipe.json")
    assert len(load_all_sessions(paths)) == 1  # returns (no block), FIFO skipped


def test_load_session_file_rejects_a_symlink(project, tmp_path):
    paths, mgr = project
    _real_session_with_report(mgr)
    external = tmp_path / "out.json"
    external.write_text('{"session_id":"deadbeef","title":"x"}', encoding="utf-8")
    (paths.sessions_dir / "deadbeef.json").symlink_to(external)
    with pytest.raises(SessionError, match="not a regular file"):
        mgr.load_session_file("deadbeef")


def test_recover_reports_unsafe_session_file_without_following(project, tmp_path):
    paths, mgr = project
    _real_session_with_report(mgr)
    external = tmp_path / "out.json"
    external.write_text("{}", encoding="utf-8")
    (paths.sessions_dir / "evil.json").symlink_to(external)
    assert "evil.json" in mgr.recover()["corrupt"]


# Reports ---------------------------------------------------------------------
def test_list_reports_skips_symlinked_and_fifo_reports(project, tmp_path):
    paths, mgr = project
    _real_session_with_report(mgr)  # one real report
    secret = tmp_path / "secret.md"
    secret.write_text("# SECRET\n", encoding="utf-8")
    (paths.reports_dir / "evil-pr.md").symlink_to(secret)
    os.mkfifo(paths.reports_dir / "fifo-pr.md")

    names = {p.name for p in list_reports(paths.reports_dir)}
    assert "evil-pr.md" not in names and "fifo-pr.md" not in names
    assert any(n.endswith("-pr.md") for n in names)  # the real report remains
    # first_title never follows the link to read the external target.
    assert first_title(paths.reports_dir / "evil-pr.md") is None


def test_cli_last_never_selects_a_symlinked_report(project, tmp_path, monkeypatch, capsys):
    paths, mgr = project
    _real_session_with_report(mgr)
    secret = tmp_path / "secret.md"
    secret.write_text("# SECRET LEAK\n", encoding="utf-8")
    # Newer than the real report, so it would win if it were not filtered out.
    (paths.reports_dir / "zzzzzzzz-pr.md").symlink_to(secret)
    monkeypatch.setattr(cli, "resolve_project_paths", lambda: paths)

    assert cli.main(["last"]) == 0
    out = capsys.readouterr().out
    assert "SECRET LEAK" not in out
    assert latest_report(paths.reports_dir).name != "zzzzzzzz-pr.md"


# The safe reader itself ------------------------------------------------------
def test_read_json_safe_reads_regular_and_rejects_non_regular(tmp_path):
    good = tmp_path / "g.json"
    good.write_text('{"k": 1}', encoding="utf-8")
    assert read_json_safe(good) == {"k": 1}

    (tmp_path / "link.json").symlink_to(good)
    with pytest.raises(OSError):
        read_json_safe(tmp_path / "link.json")

    os.mkfifo(tmp_path / "fifo.json")
    with pytest.raises(OSError):
        read_json_safe(tmp_path / "fifo.json")


def test_open_regular_binary_rejects_non_regular(tmp_path):
    # The binary reader used by dirty-file fingerprinting refuses a symlink (via
    # O_NOFOLLOW) and a FIFO (via O_NONBLOCK + fstat), closing the lstat race.
    good = tmp_path / "data.bin"
    good.write_bytes(b"abc")
    with open_regular_binary(good) as handle:
        assert handle.read() == b"abc"
    (tmp_path / "blink").symlink_to(good)
    with pytest.raises(OSError):
        open_regular_binary(tmp_path / "blink")
    os.mkfifo(tmp_path / "bfifo")
    with pytest.raises(OSError):
        open_regular_binary(tmp_path / "bfifo")
