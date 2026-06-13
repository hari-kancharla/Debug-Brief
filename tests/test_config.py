"""Tests for optional .debugbrief.toml project configuration."""

from __future__ import annotations

from debugbrief.config import _coerce, _parse_flat, load_config


def test_load_config_reads_supported_keys(tmp_path):
    (tmp_path / ".debugbrief.toml").write_text(
        'default_mode = "handoff"\ntimeout_seconds = 600\ndetail = "compact"\n',
        encoding="utf-8",
    )
    assert load_config(tmp_path) == {
        "default_mode": "handoff",
        "timeout_seconds": 600,
        "detail": "compact",
    }


def test_load_config_ignores_invalid_and_unknown_keys(tmp_path):
    (tmp_path / ".debugbrief.toml").write_text(
        'default_mode = "bogus"\ntimeout_seconds = -5\nunknown = "x"\n',
        encoding="utf-8",
    )
    assert load_config(tmp_path) == {}


def test_load_config_missing_file_is_empty(tmp_path):
    assert load_config(tmp_path) == {}


def test_load_config_malformed_file_is_ignored(tmp_path):
    (tmp_path / ".debugbrief.toml").write_text("this is = not [valid toml", encoding="utf-8")
    # Never raises; a malformed config cannot break a command.
    assert isinstance(load_config(tmp_path), dict)


def test_flat_parser_reads_scalars_and_skips_sections():
    # Exercises the Python < 3.11 fallback path directly.
    cfg = _coerce(
        _parse_flat('default_mode = "pr"\ntimeout_seconds = 120\n[ignored]\nx = 1\n')
    )
    assert cfg == {"default_mode": "pr", "timeout_seconds": 120}
