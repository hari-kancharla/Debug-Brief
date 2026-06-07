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
    assert "postgres://user:[redacted]@db:5432/app" == out


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
