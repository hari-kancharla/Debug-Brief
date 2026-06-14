"""Tests for optional .debugbrief.toml project configuration.

Parsing uses the standard-library tomllib on 3.11+ and the tomli backport on
3.9/3.10; both are the same parser, so behavior is identical across the matrix.
"""

from __future__ import annotations

import os
import sys

import pytest

from debugbrief.config import load_config, parse_error


def _write(tmp_path, text):
    (tmp_path / ".debugbrief.toml").write_text(text, encoding="utf-8")


def test_load_config_reads_supported_keys(tmp_path):
    _write(tmp_path, 'default_mode = "handoff"\ntimeout_seconds = 600\ndetail = "compact"\n')
    assert load_config(tmp_path) == {
        "default_mode": "handoff",
        "timeout_seconds": 600,
        "detail": "compact",
    }


def test_inline_comments_and_integer_underscores(tmp_path):
    _write(tmp_path, 'timeout_seconds = 1_200  # twenty minutes\ndefault_mode = "pr" # mode\n')
    assert load_config(tmp_path) == {"timeout_seconds": 1200, "default_mode": "pr"}


def test_keys_inside_a_section_are_not_top_level(tmp_path):
    # A supported key under a [section] is not top-level in TOML, so it must not
    # alter DebugBrief's settings even though the file is otherwise valid.
    _write(tmp_path, 'detail = "compact"\n[tool.other]\ntimeout_seconds = 1\n')
    assert load_config(tmp_path) == {"detail": "compact"}


def test_invalid_values_are_ignored(tmp_path):
    _write(tmp_path, 'default_mode = "bogus"\ntimeout_seconds = -5\ndetail = 3\nunknown = "x"\n')
    assert load_config(tmp_path) == {}


def test_boolean_is_not_accepted_as_timeout(tmp_path):
    # TOML true parses as a bool, which must not satisfy the integer timeout.
    _write(tmp_path, "timeout_seconds = true\n")
    assert load_config(tmp_path) == {}


def test_malformed_toml_is_ignored_as_a_whole(tmp_path):
    # A valid key followed by a syntax error: the entire file is rejected, not
    # partially applied, so the valid line is NOT silently used.
    _write(tmp_path, 'default_mode = "incident"\nthis :: is not valid toml [[[\n')
    assert load_config(tmp_path) == {}


def test_duplicate_keys_make_the_file_malformed(tmp_path):
    # Duplicate keys are a TOML error, so the whole file is ignored.
    _write(tmp_path, 'timeout_seconds = 100\ntimeout_seconds = 200\n')
    assert load_config(tmp_path) == {}


def test_missing_file_is_empty(tmp_path):
    assert load_config(tmp_path) == {}


def test_parse_error_reports_malformed_without_exposing_contents(tmp_path):
    secret = "token = sk-supersecretvalue1234 [[[ broken"
    _write(tmp_path, secret)
    msg = parse_error(tmp_path)
    assert msg is not None
    assert ".debugbrief.toml" in msg
    assert "sk-supersecretvalue1234" not in msg  # never quote file contents


def test_parse_error_is_none_for_valid_or_absent(tmp_path):
    assert parse_error(tmp_path) is None  # absent
    _write(tmp_path, 'default_mode = "pr"\n')
    assert parse_error(tmp_path) is None  # valid


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink/FIFO")
def test_symlinked_or_special_config_is_ignored_without_blocking(tmp_path):
    # load_config runs on every command; a FIFO at .debugbrief.toml must not block
    # it, and a symlink must not be followed. Both are ignored, and doctor flags it.
    os.mkfifo(tmp_path / ".debugbrief.toml")
    assert load_config(tmp_path) == {}  # returns immediately, does not block
    assert "not a regular file" in (parse_error(tmp_path) or "")


def test_invalid_utf8_is_ignored_not_crashed(tmp_path):
    # Reading non-UTF-8 raises UnicodeDecodeError before tomllib; it must be
    # handled, not crash run/redo/preview/end (load_config) or doctor (parse_error).
    (tmp_path / ".debugbrief.toml").write_bytes(b"\xff\xfe not valid utf-8")
    assert load_config(tmp_path) == {}
    msg = parse_error(tmp_path)
    assert msg is not None and ".debugbrief.toml" in msg
