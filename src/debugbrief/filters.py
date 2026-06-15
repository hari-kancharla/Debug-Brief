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


def _strip_shell_comment(command: str) -> str:
    """Drop a top-level shell comment (``#`` to end of line), quote/backslash-aware.

    A ``#`` starts a comment only at the start of a word (start of string or
    after unquoted whitespace), matching the shell, so ``pytest # a && b`` is just
    ``pytest`` and the operators in the comment do not make it look compound. A
    ``#`` inside quotes, escaped, or mid-word (``a#b``) is left alone.
    """
    in_single = in_double = False
    out: List[str] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and not in_single:
            out.append(ch)
            if i + 1 < n:
                out.append(command[i + 1])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif (
            ch == "#"
            and not in_single
            and not in_double
            and (i == 0 or command[i - 1] in " \t")
        ):
            newline = command.find("\n", i)
            if newline == -1:
                break  # comment runs to end of string
            i = newline  # keep the newline (a separator) and resume after it
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _is_shell_pipeline(command: str) -> bool:
    """True if ``command`` contains a top-level shell pipe (``|``, not ``||``).

    Quote-aware so a ``|`` inside a quoted string does not count. Conservative:
    it does not parse the full shell grammar, only enough to spot a pipeline.
    """
    command = _strip_shell_comment(command)
    in_single = in_double = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and not in_single:
            i += 2  # backslash escapes the next char, so an escaped | is literal
            continue
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
    command = _strip_shell_comment(command)
    segments: List[str] = []
    current: List[str] = []
    in_single = in_double = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and not in_single and i + 1 < n:
            current.append(ch)  # keep an escaped operator as literal text
            current.append(command[i + 1])
            i += 2
            continue
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
        i += 1
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def _contains_recognized_check(command: str) -> bool:
    """True if any segment of the command is a recognized test/build check.

    Scans each shell segment, not just the first token, so a check that follows
    setup (``cd pkg && pytest | tee``) is recognized. Used only to decide whether
    to explain why a compound command was not attributed to a tool; a compound is
    never classified as that tool.
    """
    for segment in _split_shell_segments(command):
        toks = _tokenize(segment)
        if _match_test(toks) is not None or _match_build(toks) is not None:
            return True
    return False


def _pipefail_disabled(command: str) -> bool:
    """True if the command turns ``pipefail`` off via ``set`` or ``shopt``.

    The runner enables pipefail for shell commands, but a command can disable it
    again, which makes a later pipeline's exit status unreliable, so reliability
    must account for it. Two forms disable it:

    - ``set +o pipefail`` (also ``set +eo pipefail``): the ``+``-prefixed form,
      not ``set -o pipefail`` which enables it;
    - ``shopt -u -o pipefail``: ``-o`` selects ``set -o`` option names and ``-u``
      unsets them, so this unsets pipefail (flags may be combined, e.g. ``-uo``).
      ``shopt -s -o pipefail`` enables it and does not count.
    """
    for segment in _split_shell_segments(command):
        toks = _tokenize(segment)
        if not toks:
            continue
        name = os.path.basename(toks[0])
        if name == "set":
            for i in range(1, len(toks)):
                prev = toks[i - 1]
                if toks[i] == "pipefail" and prev.startswith("+") and prev.endswith("o"):
                    return True
        elif name == "shopt":
            flags = "".join(t[1:] for t in toks[1:] if t.startswith("-"))
            args = [t for t in toks[1:] if not t.startswith("-")]
            if "u" in flags and "o" in flags and "pipefail" in args:
                return True
    return False


def _amp_is_redirection(command: str, i: int) -> bool:
    """True if the ``&`` at index ``i`` is part of a descriptor redirection.

    Covers ``&>file`` (the next char is ``>``) and ``2>&1`` / ``>&2`` / ``<&0``
    (the ``&`` follows ``>`` or ``<``). These belong to one command, so the ``&``
    is neither a stage separator nor a background operator.
    """
    prev = command[i - 1] if i > 0 else ""
    nxt = command[i + 1] if i + 1 < len(command) else ""
    return prev in (">", "<") or nxt == ">"


def _is_compound_shell(command: str) -> bool:
    """True if the command joins more than one shell stage.

    Scans for a top-level ``|`` (pipe or ``||``), ``&&``, a lone ``&``
    (background), ``;``, or newline, rather than counting segments, so a trailing
    operator (``pytest &``) is detected. A ``&`` that is part of a redirection
    (``2>&1``, ``&>log``) does not count. Quote- and backslash-aware, and a
    trailing shell comment is ignored.
    """
    command = _strip_shell_comment(command)
    in_single = in_double = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in (";", "\n", "|"):
                return True
            if ch == "&":
                if i + 1 < n and command[i + 1] == "&":
                    return True  # "&&" and-list
                if not _amp_is_redirection(command, i):
                    return True  # a lone "&" backgrounds a stage
        i += 1
    return False


def _has_shell_negation(command: str) -> bool:
    """True if a stage is prefixed with ``!``, which inverts its exit status.

    ``! pytest`` exits 0 exactly when pytest fails, so the exit code cannot be
    trusted as the check's pass/fail. A ``!`` counts as negation when it is the
    first word of a stage, including after the ``time`` reserved word
    (``time ! pytest``); a ``!`` inside an argument or quotes does not.
    """
    for segment in _split_shell_segments(command):
        toks = _tokenize(segment)
        i = 0
        # `time` (with its options) is a pipeline prefix, so a `!` after it still
        # negates the pipeline.
        if i < len(toks) and toks[i] == "time":
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
        if i < len(toks) and toks[i] == "!":
            return True
    return False


def _reliable_shell(command: str, pipefail: bool) -> bool:
    """True if the overall exit code reliably reflects every stage.

    Reliable means no shell negation (``!`` inverts the status), no
    failure-masking operator (``||``, ``;``, ``&``, newline can let the command
    exit 0 despite a failing stage), and, when a pipeline is present, ``pipefail``
    in effect and not disabled. Under these conditions an exit of 0 proves every
    stage, the check included, passed; a nonzero exit proves some stage failed,
    though not necessarily which one.
    """
    if _has_shell_negation(command):
        return False
    if _has_failure_masking_operator(command):
        return False
    if _is_shell_pipeline(command):
        return pipefail and not _pipefail_disabled(command)
    return True


def _has_failure_masking_operator(command: str) -> bool:
    """True if a top-level operator can let the command exit 0 despite a failing
    stage, so a recognized check's pass/fail cannot be read from the exit code.

    ``;`` and a newline run the next stage regardless of the previous one; ``||``
    runs its right side only when the left failed; a lone ``&`` backgrounds a
    stage. ``&&`` and a ``|`` pipe (trustworthy under pipefail) instead propagate
    failure, so they are not masking, and a ``&`` that is part of a redirection
    (``2>&1``, ``&>log``) is not an operator at all. Quote- and backslash-aware,
    and a trailing shell comment is ignored.
    """
    command = _strip_shell_comment(command)
    in_single = in_double = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and not in_single:
            i += 2  # an escaped operator is literal, not failure-masking
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in (";", "\n"):
                return True
            if ch == "|" and i + 1 < n and command[i + 1] == "|":
                return True
            if ch == "&":
                if i + 1 < n and command[i + 1] == "&":
                    i += 2  # "&&" propagates failure; not masking
                    continue
                if i > 0 and command[i - 1] == "|":
                    i += 1  # part of "|&" (pipe both streams): governed by the pipe
                    continue
                if _amp_is_redirection(command, i):
                    i += 1  # redirection "&" (2>&1, &>log): not masking
                    continue
                return True  # a lone "&" backgrounds the preceding stage
        i += 1
    return False


def shell_command_warning(
    command: str,
    use_shell: bool,
    pipefail: bool,
    passed: bool,
    force_verification: bool = False,
) -> Optional[str]:
    """Explain why a recognized check in a shell command was not counted, or None.

    Mirrors :func:`_classify_shell_command`: it returns a message only when a
    command that contains a recognized check was recorded generically (not as a
    verification), so the user is never left guessing. Simple commands and cleanly
    classified compounds return None.
    """
    if not use_shell:
        return None
    if force_verification and _has_shell_negation(command):
        # Covers a simple negated command too (which is otherwise not compound).
        return (
            "This command negates its exit status with '!', which inverts "
            "pass/fail, so --verify did not record a verification."
        )
    if not _is_compound_shell(command):
        return None
    if force_verification and _reliable_shell(command, pipefail):
        return None  # classified as a declared whole-command custom check
    if force_verification:
        # --verify was given but the compound's exit code cannot be trusted, so it
        # was not counted. Say so even when no built-in tool is recognized.
        return (
            "This command was declared with --verify, but its exit code cannot be "
            "trusted (an unreliable pipeline without pipefail, or a failure-masking "
            "operator like ||, ;, or &), so it was not recorded as a verification."
        )
    if not _contains_recognized_check(command):
        return None  # nothing looked like a check, so nothing to explain
    if not passed:
        return (
            "Compound shell command failed; DebugBrief cannot determine which "
            "stage failed, so it is recorded as a command, not a failed check."
        )
    return (
        "DebugBrief records a compound shell command as a single command and does "
        "not attribute the result to an individual tool. Run the check on its own, "
        "or pass --verify to record the whole command as a check."
    )


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

    A simple shell command (one stage) is classified like a non-shell command. A
    compound shell command (anything joined by ``|``, ``&&``, ``||``, ``;``,
    ``&`` or a newline) is never attributed to an internal tool: the exit code
    does not say which stage produced it. The only verification a compound can
    yield is a user-declared whole-command check (``--verify``), and only when the
    exit code is reliable: no failure-masking operator, and ``pipefail`` for a
    pipeline. Everything else is recorded as a generic command.
    """
    status = status_from_outcome(
        exit_code, timed_out, errored, interrupted, broken_pipe
    )
    passed = status == COMMAND_STATUS_PASSED

    if use_shell:
        return _classify_shell_command(
            command, status, passed, pipefail, force_verification
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


def _classify_shell_command(
    command: str,
    status: str,
    passed: bool,
    pipefail: bool,
    force_verification: bool,
) -> CommandClassification:
    """Classify a command run through the shell, trusting the exit code only when
    it reliably reflects a recognized check (see :func:`classify_command`)."""
    generic = CommandClassification(
        is_test=False, is_verification=False, tool=None, status=status
    )

    # A simple command (one stage) is classified directly: the exit code is its
    # own, so a recognized check passes or fails honestly.
    if not _is_compound_shell(command):
        toks = _tokenize(command)
        test_tool = _match_test(toks)
        if test_tool is not None:
            return CommandClassification(
                is_test=True, is_verification=passed, tool=test_tool, status=status
            )
        build_match = _match_build(toks)
        if build_match is not None:
            return CommandClassification(
                is_test=False, is_verification=passed, tool=build_match[0], status=status
            )
        # --verify declares an unrecognized command a custom check, but not when
        # its exit code is inverted by a leading "!" (see _reliable_shell).
        if force_verification and _reliable_shell(command, pipefail):
            return CommandClassification(
                is_test=False, is_verification=passed, tool="custom", status=status
            )
        return generic

    # Compound command: never attribute the result to an internal tool, because
    # the exit code does not say which stage produced it (pytest may not have run,
    # or may have passed while another stage failed). The only verification a
    # compound can yield is a user-declared whole-command check via --verify, and
    # only when the exit code is reliable (no failure-masking operator, and a
    # pipeline has pipefail). Everything else is a generic command.
    if force_verification and _reliable_shell(command, pipefail):
        return CommandClassification(
            is_test=False, is_verification=passed, tool="custom", status=status
        )
    return generic


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
