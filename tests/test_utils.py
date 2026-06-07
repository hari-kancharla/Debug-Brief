"""Tests for small shared helpers in :mod:`debugbrief.utils`."""

from __future__ import annotations

from debugbrief.utils import truncate_text


def test_truncate_no_limit_returns_text_unchanged():
    text = "some output"
    assert truncate_text(text, 0) == (text, False)
    assert truncate_text(text, -5) == (text, False)


def test_truncate_shorter_than_limit_is_untouched():
    text = "short"
    assert truncate_text(text, 100) == (text, False)


def test_truncate_keeps_head_and_tail():
    # Distinct head and tail so we can prove both survive the elision.
    text = "HEAD" + ("m" * 200) + "TAIL"
    truncated, was_truncated = truncate_text(text, 20)
    assert was_truncated is True
    # The beginning and the end of the original output are both preserved.
    assert truncated.startswith("HEAD")
    assert truncated.endswith("TAIL")
    # An elision marker sits between the kept head and tail.
    assert "omitted" in truncated
    assert "..." in truncated


def test_truncate_tail_preserves_trailing_error():
    # A traceback-like error at the end must not be discarded.
    body = "noise line\n" * 500
    text = body + "Traceback: ValueError at the very end"
    truncated, was_truncated = truncate_text(text, 40)
    assert was_truncated is True
    assert truncated.endswith("at the very end")


def test_truncate_keeps_both_head_and_tail_markers():
    # The decisive error is at the very end; the head is also kept.
    text = "HEAD-START " + ("filler " * 1000) + "TAIL-ERROR"
    truncated, was_truncated = truncate_text(text, 120)
    assert was_truncated is True
    assert truncated.startswith("HEAD-START")
    assert truncated.endswith("TAIL-ERROR")
    assert "characters omitted" in truncated


def test_truncate_keeps_limit_worth_of_original_characters():
    text = "x" * 1000
    limit = 50
    truncated, was_truncated = truncate_text(text, limit)
    assert was_truncated is True
    # Head + tail together preserve exactly `limit` original characters; the
    # marker is additional, so the result is longer than the limit.
    assert truncated.count("x") == limit
    assert len(truncated) > limit
    assert f"{1000 - limit} characters omitted" in truncated
