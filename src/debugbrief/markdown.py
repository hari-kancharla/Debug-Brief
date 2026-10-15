"""Helpers for emitting Markdown that stays valid for arbitrary content.

Commands, filenames, and captured output are untrusted: a command or filename
can contain a backtick, and captured output can contain a line of backticks that
would close a code fence early. These helpers choose a delimiter long enough that
the content cannot break out, following the CommonMark rules.
"""

from __future__ import annotations

import html
import re
from typing import Any

_MARKDOWN_META = re.compile(r"([\\`*_{}\[\]|~])")
_MARKDOWN_PATH_META = re.compile(r"([\\`*\[\]{}|~])")
_MARKDOWN_PATH_UNDERSCORE = re.compile(r"(?<![\w])_(?![\w])")
_MARKDOWN_LIST = re.compile(r"^([#>+-])")
_MARKDOWN_NUMBERED = re.compile(r"^(\d+)([.)])(?=\s)")


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def normalize_text(value: Any) -> str:
    """Remove unsafe controls while preserving line boundaries."""
    text = _stringify(value).replace("\r\n", "\n").replace("\r", "\n")
    normalized: list[str] = []
    for character in text:
        if character in "\n\t" or character.isprintable():
            normalized.append(character)
        elif character.isspace():
            normalized.append(" ")
        else:
            normalized.append(" ")
    return "".join(normalized)


def normalize_plain_text(value: Any) -> str:
    """Return normalized text without Markdown or HTML escaping."""
    return normalize_text(value)


def normalize_free_text(value: Any) -> str:
    """Return normalized plain text on one line."""
    return " ".join(normalize_text(value).split())


def _escape_markdown_line(line: str) -> str:
    escaped = html.escape(line, quote=False)
    escaped = _MARKDOWN_META.sub(r"\\\1", escaped)
    escaped = _MARKDOWN_LIST.sub(r"\\\1", escaped)
    return _MARKDOWN_NUMBERED.sub(r"\1\\\2", escaped)


def inline_text(text: Any) -> str:
    """Escape normalized prose for ordinary Markdown, retaining line breaks."""
    normalized = normalize_text(text)
    if not normalized:
        return ""
    return "<br>".join(_escape_markdown_line(line) for line in normalized.split("\n"))


def inline_path(text: Any) -> str:
    """Escape a path for prose without changing ordinary filename characters."""
    normalized = normalize_text(text).replace("\n", " ")
    escaped = html.escape(normalized, quote=False)
    escaped = _MARKDOWN_PATH_META.sub(r"\\\1", escaped)
    escaped = _MARKDOWN_PATH_UNDERSCORE.sub(r"\\_", escaped)
    escaped = _MARKDOWN_LIST.sub(r"\\\1", escaped)
    return _MARKDOWN_NUMBERED.sub(r"\1\\\2", escaped)


def plain_markdown(value: Any) -> str:
    text = normalize_free_text(value)
    text = re.sub(r"\\([\\`*_{}\[\]|~#>+\-.])", r"\1", text)
    output: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "`":
            output.append(text[index])
            index += 1
            continue
        run = 1
        while index + run < len(text) and text[index + run] == "`":
            run += 1
        delimiter = "`" * run
        closing = text.find(delimiter, index + run)
        if closing == -1:
            output.append(text[index : index + run])
            index += run
            continue
        content = text[index + run : closing]
        if run > 1 and content.startswith(" ") and content.endswith(" "):
            content = content[1:-1]
        output.append(content)
        index = closing + run
    return html.unescape("".join(output))


def _longest_run(text: str, ch: str) -> int:
    longest = run = 0
    for c in text:
        if c == ch:
            run += 1
            if run > longest:
                longest = run
        else:
            run = 0
    return longest


def code_span(text: Any) -> str:
    """Render text as an inline Markdown code span, safe for any content.

    The backtick delimiter is made longer than the longest backtick run inside
    the text. A space is padded on both sides when the content starts or ends
    with a backtick or a space, so CommonMark's space-stripping rule recovers
    the original content rather than mangling the delimiter or trimming a real
    space.
    """
    text = normalize_text(text).replace("\n", " ").replace("\t", " ")
    delim = "`" * (_longest_run(text, "`") + 1)
    if text == "":
        return f"{delim}  {delim}"
    needs_pad = text[0] in ("`", " ") or text[-1] in ("`", " ")
    if needs_pad:
        return f"{delim} {text} {delim}"
    return f"{delim}{text}{delim}"


def fenced_code(text: Any) -> str:
    """Render text as a fenced Markdown code block, safe for any content.

    The fence is made longer than the longest backtick run inside the text, so
    a line of backticks in the content cannot close the block early.
    """
    text = normalize_text(text)
    fence = "`" * max(3, _longest_run(text, "`") + 1)
    return f"{fence}\n{text}\n{fence}"
