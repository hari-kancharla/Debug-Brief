"""Command classification, report-noise filtering, and deduplication.

This module is deterministic and dependency-free. It never guesses intent: it
only recognizes well-known tool invocations by their token patterns and reports
pass/fail strictly from real exit codes.
"""

from __future__ import annotations

import os.path
import shlex
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .models import (
    COMMAND_STATUS_BROKEN_PIPE,
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
    (["gradlew", "test"], "gradle"),
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

# Wrappers that delegate to an inner command. Recognition unwraps them so the
# real tool underneath is what gets matched.
_RUN_WRAPPERS = {"uv", "poetry", "pdm", "hatch", "rye"}  # "<tool> run <cmd ...>"
_EXEC_WRAPPERS = {"pnpm", "yarn"}  # "<tool> exec|dlx <cmd ...>"

# Long options of the supported wrappers that are known to take NO value. Any
# other long option is assumed to consume the following token as its value, so an
# option's value is never mistaken for the command. That default is the safe one:
# guessing "takes a value" can at most skip the real command (a missed check),
# never promote a non-test to a passed test. Listing a flag here is only an
# optimization to recognize the common boolean forms (npx --yes jest); a flag we
# fail to list just falls back to the safe default. Every entry must be a genuine
# boolean in its tool, or it could hide a real command behind it.
_BOOLEAN_FLAGS = frozenset(
    {
        # npx / bunx
        "--yes", "--no-install", "--prefer-offline", "--prefer-online",
        "--offline", "--ignore-existing", "--quiet",
        # uv run
        "--no-sync", "--frozen", "--locked", "--no-dev", "--dev", "--all-extras",
        "--no-editable", "--isolated", "--system", "--no-project", "--refresh",
        "--reinstall", "--native-tls", "--no-cache", "--verbose",
        # poetry / pdm / hatch / rye
        "--no-interaction", "--sync", "--no-root",
    }
)

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


def _is_env_assignment(token: str) -> bool:
    """True for a leading ``NAME=value`` shell environment assignment."""
    eq = token.find("=")
    if eq <= 0:
        return False
    name = token[:eq]
    return (name[0].isalpha() or name[0] == "_") and all(
        c.isalnum() or c == "_" for c in name
    )


def _is_python(name: str) -> bool:
    return name in ("python", "python2", "python3") or name.startswith(
        ("python2.", "python3.")
    )


def _skip_wrapper_options(toks: List[str]) -> List[str]:
    """Drop a wrapper's own options so the wrapped command surfaces.

    Wrappers take options before the command, and many consume the following
    token as a value. Arities are not known in general, so the rule errs toward
    consuming a value, which never mistakes an option's value for the command (a
    false-positive classification, the dangerous direction for an honest tool):

    - ``--`` ends option processing; the rest is the command.
    - ``--name=value`` is self-contained.
    - a known boolean long flag (:data:`_BOOLEAN_FLAGS`) consumes nothing, so a
      flag before the command is handled (``npx --yes jest``,
      ``uv run --no-sync pytest``).
    - any other long option consumes the next token as its value, so an unknown
      value option (``uv run --env-file .env pytest``) cannot leave its value to
      be read as the command. The cost is only a missed check when an unlisted
      long flag is actually boolean, which ``--verify`` covers.
    - a short option (``-q``) is treated as a boolean flag and consumes nothing,
      so ``poetry run -q pytest`` still surfaces the command.
    """
    while toks and toks[0].startswith("-"):
        opt = toks[0]
        if opt == "--":
            return toks[1:]
        toks = toks[1:]
        name = opt.split("=", 1)[0]
        takes_value = (
            opt.startswith("--")
            and "=" not in opt
            and name not in _BOOLEAN_FLAGS
        )
        if takes_value and toks and not toks[0].startswith("-"):
            toks = toks[1:]  # consume the long option's value
    return toks


def _effective_tokens(tokens: List[str]) -> List[str]:
    """Resolve a command to its effective ``[executable, args...]`` form.

    Strips leading ``NAME=value`` environment assignments and unwraps common
    runner wrappers (``python -m``, ``uv``/``poetry``/``pdm``/``hatch``/``rye``
    ``run``, ``bundle exec``, ``npx``/``bunx``, ``pnpm``/``yarn`` ``exec``), then
    reduces the executable to its basename so ``.venv/bin/pytest`` resolves to
    ``pytest``. Recognition is anchored to this executable, so a tool name that
    only appears as an argument (``echo pytest``) is not mistaken for the tool.
    """
    toks = list(tokens)
    while toks and _is_env_assignment(toks[0]):
        toks = toks[1:]
    for _ in range(8):  # bounded: unwrap nested wrappers like "uv run python -m pytest"
        if not toks:
            break
        head = os.path.basename(toks[0])
        if _is_python(head) and len(toks) >= 3 and toks[1] == "-m":
            toks = toks[2:]
            continue
        if head in _RUN_WRAPPERS and len(toks) >= 2 and toks[1] == "run":
            toks = _skip_wrapper_options(toks[2:])
            continue
        if head == "bundle" and len(toks) >= 2 and toks[1] == "exec":
            toks = _skip_wrapper_options(toks[2:])
            continue
        if head in ("npx", "bunx") and len(toks) >= 2:
            toks = _skip_wrapper_options(toks[1:])
            continue
        if head in _EXEC_WRAPPERS and len(toks) >= 2 and toks[1] in ("exec", "dlx"):
            toks = _skip_wrapper_options(toks[2:])
            continue
        break
    if toks:
        toks = [os.path.basename(toks[0])] + toks[1:]
    return toks


def _match_test(tokens: List[str]) -> Optional[str]:
    eff = _effective_tokens(tokens)
    for pattern, tool in _TEST_PATTERNS:
        if eff[: len(pattern)] == pattern:
            return tool
    return None


def _match_build(tokens: List[str]) -> Optional[Tuple[str, str]]:
    eff = _effective_tokens(tokens)
    for pattern, tool, category in _BUILD_PATTERNS:
        if eff[: len(pattern)] == pattern:
            return tool, category
    return None


def _is_shell_pipeline(command: str) -> bool:
    """True if ``command`` contains a top-level shell pipe (``|``, not ``||``).

    Quote-aware so a ``|`` inside a quoted string does not count. Conservative:
    it does not parse the full shell grammar, only enough to spot a pipeline.
    """
    in_single = in_double = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "|" and not in_single and not in_double:
            prev_pipe = i > 0 and command[i - 1] == "|"
            next_pipe = i + 1 < n and command[i + 1] == "|"
            if not prev_pipe and not next_pipe:
                return True
        i += 1
    return False


def _split_shell_segments(command: str) -> List[str]:
    """Split a command into its shell segments on top-level separators.

    Breaks on ``;``, ``&``, ``|`` and newlines (so ``&&`` / ``||`` / ``|`` and
    command terminators all end a segment), staying quote-aware so a separator
    inside a quoted string does not split. Lets a recognized check be found even
    when it does not sit at the very first token (``cd pkg && pytest | tee``).
    """
    segments: List[str] = []
    current: List[str] = []
    in_single = in_double = False
    for ch in command:
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
        elif ch in (";", "&", "|", "\n") and not in_single and not in_double:
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
        else:
            current.append(ch)
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def _contains_recognized_check(command: str) -> bool:
    """True if any segment of the command is a recognized test/build check."""
    for segment in _split_shell_segments(command):
        toks = _tokenize(segment)
        if _match_test(toks) is not None or _match_build(toks) is not None:
            return True
    return False


def shell_pipeline_suppressed_check(
    command: str, use_shell: bool, pipefail: bool = False
) -> bool:
    """True when an unreliable pipeline kept a would-be check from classifying.

    Only when ``pipefail`` is unavailable is a pipeline's exit status untrustworthy
    (it reflects only the last stage). The runner uses this to warn that a
    recognized check inside such a pipeline is not treated as a verification. With
    pipefail the exit status is reliable and the check classifies normally.

    The check is looked for in every segment, not just the first token, so a
    setup-prefixed pipeline (``cd pkg && pytest | tee``) is recognized and warned
    about rather than dropped silently.
    """
    if pipefail or not (use_shell and _is_shell_pipeline(command)):
        return False
    return _contains_recognized_check(command)


def status_from_outcome(
    exit_code: Optional[int],
    timed_out: bool,
    errored: bool,
    interrupted: bool = False,
    broken_pipe: bool = False,
) -> str:
    """Map an execution outcome to a command status string."""
    if interrupted:
        return COMMAND_STATUS_INTERRUPTED
    if broken_pipe:
        return COMMAND_STATUS_BROKEN_PIPE
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
    broken_pipe: bool = False,
    use_shell: bool = False,
    pipefail: bool = False,
) -> CommandClassification:
    """Classify a command into test / verification categories.

    A command is verification-worthy only if it is a recognized test command
    that exited 0, or a recognized build/lint/typecheck command that exited 0.
    Pass/fail is derived strictly from the real exit code.

    ``force_verification`` lets the user declare an unrecognized command (a
    custom test script, ``make integration``) as a check. It applies only when
    no pattern matched; a recognized runner always wins. The honesty rule is
    unchanged: ``is_verification`` is True only on a real exit 0.

    A shell pipeline (``use_shell`` with a top-level ``|``) is classified as a
    check only when ``pipefail`` is in effect, so its exit status reflects the
    first failing stage. Without pipefail the exit status is only the last
    stage's, so the pipeline is not treated as a check (it could otherwise record
    a failed test as passed); the runner attaches a warning in that case.
    """
    status = status_from_outcome(
        exit_code, timed_out, errored, interrupted, broken_pipe
    )
    passed = status == COMMAND_STATUS_PASSED

    if use_shell and not pipefail and _is_shell_pipeline(command):
        return CommandClassification(
            is_test=False, is_verification=False, tool=None, status=status
        )

    tokens = _tokenize(command)
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
    invocation_cwd: Optional[str] = None

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
            # A command is the same check only when run from the same directory;
            # the same string in two directories is two different checks.
            if (
                existing.command != command_text
                or existing.invocation_cwd != data.invocation_cwd
            ):
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
                invocation_cwd=data.invocation_cwd,
            )
        )

    return results
