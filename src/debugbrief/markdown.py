"""Helpers for emitting Markdown that stays valid for arbitrary content.

Commands, filenames, and captured output are untrusted: a command or filename
can contain a backtick, and captured output can contain a line of backticks that
would close a code fence early. These helpers choose a delimiter long enough that
the content cannot break out, following the CommonMark rules.
"""

from __future__ import annotations


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


def code_span(text: str) -> str:
    """Render ``text`` as an inline Markdown code span, safe for any content.

    The backtick delimiter is made longer than the longest backtick run inside
    the text. A space is padded on both sides when the content starts or ends
    with a backtick or a space, so CommonMark's space-stripping rule recovers the
    original content rather than mangling the delimiter or trimming a real space.
    """
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    delim = "`" * (_longest_run(text, "`") + 1)
    if text == "":
        return f"{delim}  {delim}"
    needs_pad = text[0] in ("`", " ") or text[-1] in ("`", " ")
    if needs_pad:
        return f"{delim} {text} {delim}"
    return f"{delim}{text}{delim}"


def fenced_code(text: str) -> str:
    """Render ``text`` as a fenced Markdown code block, safe for any content.

    The fence is made longer than the longest backtick run inside the text, so a
    line of backticks in the content cannot close the block early.
    """
    fence = "`" * max(3, _longest_run(text, "`") + 1)
    return f"{fence}\n{text}\n{fence}"
