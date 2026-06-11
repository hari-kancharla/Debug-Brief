"""Tests for secret redaction at capture time."""

from __future__ import annotations

import sys

from debugbrief.command_runner import run_command
from debugbrief.redaction import redact_text

PY = sys.executable


def test_redacts_openai_style_key():
    out, n = redact_text("token is sk-abcdEFGH1234567890 ok")
    assert "sk-abcdEFGH" not in out
    assert "[redacted]" in out
    assert n >= 1


def test_redacts_authorization_header():
    out, n = redact_text("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "[redacted]" in out
    assert n >= 1


def test_redacts_key_value_pair():
    out, _ = redact_text("API_KEY=supersecretvalue")
    assert "supersecretvalue" not in out
    assert out == "API_KEY=[redacted]"


def test_redacts_aws_and_github_tokens():
    out, _ = redact_text("id AKIAIOSFODNN7EXAMPLE gh ghp_0123456789abcdefABCDEF0123456789")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "ghp_0123456789" not in out


def test_connection_string_masks_only_password():
    out, _ = redact_text("postgres://user:hunter2@db:5432/app")
    assert "hunter2" not in out
    assert out == "postgres://user:[redacted]@db:5432/app"


def test_private_key_block_masked():
    block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAKj...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out, n = redact_text(block)
    assert "MIIBOgIBAAJB" not in out
    assert out == "[redacted]"
    assert n == 1


def test_no_false_positive_on_plain_text():
    out, n = redact_text("just a normal line with print(123) and retry logic")
    assert n == 0
    assert out == "just a normal line with print(123) and retry logic"


def test_embedded_sensitive_substrings_are_not_redacted():
    # "key" inside "monkey"/"keyboard" and "api" inside "rapid"/"apiary" are
    # substrings, not whole key segments, so these must survive untouched.
    cases = [
        "monkey: banana",
        "turkey_count = 5",
        "donkey: 7",
        "lowkey: vibe",
        "rapid_mode = true",
        "apiary location: north",
        "keyboard = mechanical",
        "therapist=alice",
    ]
    for text in cases:
        out, n = redact_text(text)
        assert out == text, f"unexpectedly modified: {text!r} -> {out!r}"
        assert n == 0, f"unexpected redaction in: {text!r}"


def test_sensitive_segments_are_redacted():
    # The sensitive token appears as a full, separator-delimited segment (or the
    # whole key), so the value must be masked.
    cases = [
        ("password=hunter2", "password=[redacted]"),
        ("passwd: hunter2", "passwd: [redacted]"),
        ("pwd=hunter2", "pwd=[redacted]"),
        ("API_KEY=abc123", "API_KEY=[redacted]"),
        ("api_key: abc123", "api_key: [redacted]"),
        ("api-key=abc123", "api-key=[redacted]"),
        ("apikey=abc123", "apikey=[redacted]"),
        ("secret=abc123", "secret=[redacted]"),
        ("session_token=abc123", "session_token=[redacted]"),
        ("aws_secret_access_key=wJalrXUtnFEMI", "aws_secret_access_key=[redacted]"),
        ("key=abc123", "key=[redacted]"),
        ('password="hunter2"', 'password="[redacted]"'),
    ]
    for text, expected in cases:
        out, n = redact_text(text)
        assert out == expected, f"{text!r} -> {out!r}"
        assert n == 1


def test_stored_event_is_redacted_by_default(tmp_path):
    # The command prints a fake secret on stdout; the stored preview must mask it.
    result = run_command(
        f"{PY} -c \"print('API_KEY=supersecretvalue')\"", cwd=tmp_path
    )
    assert "supersecretvalue" not in result.command_data.stdout_preview
    assert "[redacted]" in result.command_data.stdout_preview
    assert result.command_data.redacted is True


def test_no_redact_stores_raw(tmp_path):
    result = run_command(
        f"{PY} -c \"print('API_KEY=supersecretvalue')\"",
        cwd=tmp_path,
        redact=False,
    )
    assert "supersecretvalue" in result.command_data.stdout_preview
    assert result.command_data.redacted is False


def test_command_text_redacted_in_event(tmp_path):
    # A secret embedded in the command itself is masked in the stored command.
    result = run_command(
        f"{PY} -c \"import os; print('ok')\" # token=ghp_0123456789abcdefABCDEF0123456789",
        cwd=tmp_path,
        use_shell=True,
    )
    assert "ghp_0123456789" not in result.command_data.command
    assert result.command_data.redacted is True


def test_note_is_redacted_on_disk(tmp_path):
    # A secret pasted into a free-text note must be scrubbed before it is
    # written to the session file, the same as captured command output.
    from debugbrief.paths import ProjectPaths
    from debugbrief.session_manager import SessionManager
    from debugbrief.utils import read_json

    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    manager = SessionManager(paths)
    session = manager.start("note redaction")
    manager.add_note("rotate api_key=supersecretvalue123 before the deploy")

    raw = read_json(paths.session_file(session.session_id))
    notes = [e for e in raw["events"] if e["type"] == "note"]
    assert notes, "expected a note event on disk"
    stored = notes[-1]["data"]["text"]
    assert "supersecretvalue123" not in stored
    assert "[redacted]" in stored


def test_redacted_note_triggers_report_notice(tmp_path):
    # A redacted note (with no commands at all) must still surface the report's
    # redaction notice, not only redacted command output.
    from debugbrief.paths import ProjectPaths
    from debugbrief.reporters import render_report
    from debugbrief.session_manager import SessionManager

    paths = ProjectPaths(project_root=tmp_path, is_git_repo=False, repo_root=None)
    manager = SessionManager(paths)
    manager.start("note notice")
    manager.add_note("api_key=supersecretvalue123 rotate me")
    session = manager.load_active()

    report = render_report(session, "pr")
    assert "## Warnings and limitations" in report
    assert "Secret-like values in captured output, commands, or notes" in report
    assert "supersecretvalue123" not in report


def test_redaction_is_linear_on_long_unbroken_text():
    # Long unbroken alphanumeric runs (a pasted log line, base64, minified JS)
    # must redact in linear time. The earlier lazy key-prefix scan was
    # quadratic: 200k characters took minutes; linear takes milliseconds. The
    # generous bound keeps this stable on slow CI runners while still failing
    # decisively if the quadratic behavior ever returns.
    import time

    text = "x" * 200000
    start = time.perf_counter()
    out, count = redact_text(text)
    elapsed = time.perf_counter() - start
    assert out == text
    assert count == 0
    assert elapsed < 2.0, f"redaction took {elapsed:.2f}s on 200k chars"
