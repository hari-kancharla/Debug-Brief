"""Command classification, report-noise filtering, and deduplication.

This module is deterministic and dependency-free. It never guesses intent: it
only recognizes well-known tool invocations by their token patterns and reports
pass/fail strictly from real exit codes.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .models import (
    COMMAND_STATUS_ERROR,
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_INTERRUPTED,
    COMMAND_STATUS_PASSED,
    COMMAND_STATUS_TIMED_OUT,
    NON_SUCCESS_STATUSES,
    CommandClassification,
    CommandData,
    Event,
)
from .utils import parse_iso8601

# Default window (seconds) within which identical commands are squashed in reports.
DEFAULT_DEDUP_WINDOW_SECONDS = 30

# Low-value commands dropped from reports unless they failed.
_NOISE_SINGLE = {"ls", "ll", "pwd", "cd", "clear", "history", "cat"}
_NOISE_PAIRS = {("git", "status")}

# (pattern_tokens, tool) for test-command detection.
_TEST_PATTERNS: List[Tuple[List[str], str]] = [
    (["pytest"], "pytest"),
    (["py.test"], "pytest"),
    (["npm", "test"], "npm"),
    (["npm", "run", "test"], "npm"),
    (["pnpm", "test"], "pnpm"),
    (["pnpm", "run", "test"], "pnpm"),
    (["yarn", "test"], "yarn"),
    (["yarn", "run", "test"], "yarn"),
    (["jest"], "jest"),
    (["go", "test"], "go"),
    (["cargo", "test"], "cargo"),
    (["rspec"], "rspec"),
    (["bundle", "exec", "rspec"], "rspec"),
    (["mvn", "test"], "maven"),
    (["gradle", "test"], "gradle"),
    (["./gradlew", "test"], "gradle"),
    (["vitest"], "vitest"),
    (["bun", "test"], "bun"),
    (["deno", "test"], "deno"),
    (["node", "--test"], "node"),
    (["make", "test"], "make"),
    (["make", "check"], "make"),
    (["tox"], "tox"),
    (["unittest"], "unittest"),
    (["dotnet", "test"], "dotnet"),
    (["ctest"], "ctest"),
    (["phpunit"], "phpunit"),
    (["mix", "test"], "mix"),
    (["swift", "test"], "swift"),
]

# (pattern_tokens, tool, category) for build/lint/typecheck detection.
_BUILD_PATTERNS: List[Tuple[List[str], str, str]] = [
    (["npm", "run", "build"], "npm", "build"),
    (["pnpm", "build"], "pnpm", "build"),
    (["pnpm", "run", "build"], "pnpm", "build"),
    (["yarn", "build"], "yarn", "build"),
    (["npm", "run", "lint"], "npm", "lint"),
    (["pnpm", "lint"], "pnpm", "lint"),
    (["pnpm", "run", "lint"], "pnpm", "lint"),
    (["yarn", "lint"], "yarn", "lint"),
    (["npm", "run", "typecheck"], "npm", "typecheck"),
    (["pnpm", "typecheck"], "pnpm", "typecheck"),
    (["pnpm", "run", "typecheck"], "pnpm", "typecheck"),
    (["yarn", "typecheck"], "yarn", "typecheck"),
    (["mypy"], "mypy", "typecheck"),
    (["ruff", "check"], "ruff", "lint"),
    (["black", "--check"], "black", "lint"),
    (["tsc", "--noEmit"], "tsc", "typecheck"),
]


def _tokenize(command: str) -> List[str]:
    """Tokenize a command string, tolerating shlex parse errors."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _contains_subsequence(tokens: List[str], pattern: List[str]) -> bool:
    """Return True if ``pattern`` appears as a contiguous run inside ``tokens``."""
    if not pattern or len(pattern) > len(tokens):
        return False
    for start in range(len(tokens) - len(pattern) + 1):
        if tokens[start : start + len(pattern)] == pattern:
            return True
    return False


def _match_test(tokens: List[str]) -> Optional[str]:
    for pattern, tool in _TEST_PATTERNS:
        if _contains_subsequence(tokens, pattern):
            return tool
    return None


def _match_build(tokens: List[str]) -> Optional[Tuple[str, str]]:
    for pattern, tool, category in _BUILD_PATTERNS:
        if _contains_subsequence(tokens, pattern):
            return tool, category
    return None


def status_from_outcome(
    exit_code: Optional[int],
    timed_out: bool,
    errored: bool,
    interrupted: bool = False,
) -> str:
    """Map an execution outcome to a command status string."""
    if interrupted:
        return COMMAND_STATUS_INTERRUPTED
    if timed_out:
        return COMMAND_STATUS_TIMED_OUT
    if errored:
        return COMMAND_STATUS_ERROR
    if exit_code == 0:
        return COMMAND_STATUS_PASSED
    return COMMAND_STATUS_FAILED


def classify_command(
    command: str,
    exit_code: Optional[int],
    timed_out: bool = False,
    errored: bool = False,
    force_verification: bool = False,
    interrupted: bool = False,
) -> CommandClassification:
    """Classify a command into test / verification categories.

    A command is verification-worthy only if it is a recognized test command
    that exited 0, or a recognized build/lint/typecheck command that exited 0.
    Pass/fail is derived strictly from the real exit code.

    ``force_verification`` lets the user declare an unrecognized command (a
    custom test script, ``make integration``) as a check. It applies only when
    no pattern matched; a recognized runner always wins. The honesty rule is
    unchanged: ``is_verification`` is True only on a real exit 0.
    """
    tokens = _tokenize(command)
    status = status_from_outcome(exit_code, timed_out, errored, interrupted)
    passed = status == COMMAND_STATUS_PASSED

    test_tool = _match_test(tokens)
    if test_tool is not None:
        return CommandClassification(
            is_test=True,
            is_verification=passed,
            tool=test_tool,
            status=status,
        )

    build_match = _match_build(tokens)
    if build_match is not None:
        tool, _category = build_match
        return CommandClassification(
            is_test=False,
            is_verification=passed,
            tool=tool,
            status=status,
        )

    if force_verification:
        return CommandClassification(
            is_test=False,
            is_verification=passed,
            tool="custom",
            status=status,
        )

    return CommandClassification(
        is_test=False,
        is_verification=False,
        tool=None,
        status=status,
    )


def is_noise_command(command: str) -> bool:
    """Return True for low-value commands that reports should usually omit."""
    stripped = command.strip()
    if not stripped:
        return True
    tokens = stripped.split()
    if not tokens:
        return True
    if tokens[0] in _NOISE_SINGLE:
        return True
    return len(tokens) >= 2 and (tokens[0], tokens[1]) in _NOISE_PAIRS


@dataclass
class ReportCommand:
    """A command as it should appear in a report, after dedup/filtering."""

    command: str
    count: int
    first_timestamp: str
    last_timestamp: str
    exit_code: Optional[int]
    status: str
    is_test: bool
    is_verification: bool
    tool: Optional[str]
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stderr_preview: str = ""

    @property
    def failed(self) -> bool:
        return self.status in NON_SUCCESS_STATUSES


def _event_seconds(event: Event) -> float:
    try:
        return parse_iso8601(event.timestamp).timestamp()
    except (ValueError, TypeError):
        return 0.0


def build_report_commands(
    command_events: List[Event],
    dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
    drop_noise: bool = True,
) -> List[ReportCommand]:
    """Turn raw command events into a concise, deduplicated report list.

    - Noise commands (ls, cd, git status, ...) are dropped unless they failed.
    - Identical commands within ``dedup_window_seconds`` are squashed, tracking
      a count and keeping the most recent timestamp and outcome.
    """
    ordered = sorted(command_events, key=_event_seconds)
    results: List[ReportCommand] = []

    for event in ordered:
        data = CommandData.from_dict(event.data)
        command_text = data.command
        cls = data.classification
        status = cls.status
        is_failure = status in NON_SUCCESS_STATUSES

        if drop_noise and is_noise_command(command_text) and not is_failure:
            continue

        ts = event.timestamp
        ts_seconds = _event_seconds(event)

        merged = False
        for existing in results:
            if existing.command != command_text:
                continue
            try:
                gap = ts_seconds - parse_iso8601(existing.last_timestamp).timestamp()
            except (ValueError, TypeError):
                gap = dedup_window_seconds + 1
            if 0 <= gap <= dedup_window_seconds:
                existing.count += 1
                existing.last_timestamp = ts
                existing.exit_code = data.exit_code
                existing.status = status
                existing.is_test = cls.is_test
                existing.is_verification = cls.is_verification
                existing.tool = cls.tool
                existing.stdout_truncated = data.stdout_truncated
                existing.stderr_truncated = data.stderr_truncated
                existing.stderr_preview = data.stderr_preview
                merged = True
                break

        if merged:
            continue

        results.append(
            ReportCommand(
                command=command_text,
                count=1,
                first_timestamp=ts,
                last_timestamp=ts,
                exit_code=data.exit_code,
                status=status,
                is_test=cls.is_test,
                is_verification=cls.is_verification,
                tool=cls.tool,
                stdout_truncated=data.stdout_truncated,
                stderr_truncated=data.stderr_truncated,
                stderr_preview=data.stderr_preview,
            )
        )

    return results
