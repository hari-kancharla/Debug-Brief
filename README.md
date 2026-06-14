# DebugBrief

[![CI](https://github.com/harihkk/Debug-Brief/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/harihkk/Debug-Brief/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/debugbrief?cacheSeconds=3600)](https://pypi.org/project/debugbrief/)

![A failing test streams live, the fix lands, redo passes, and the generated brief appears](https://raw.githubusercontent.com/harihkk/Debug-Brief/main/docs/demo.gif)

Turn a debugging session into an honest markdown brief for a PR, a handoff, or an
incident note.

You record notes and run commands through DebugBrief while you work. When you are
done, it writes a report built only from what actually happened: what you tried,
what failed, what then passed, and which files changed in between. It never
invents a root cause and never claims a test result you did not get.

It is local-first: standard library plus native `git`, with one conditional
dependency (`tomli` on Python < 3.11, for reading `.debugbrief.toml`; 3.11+ uses
the standard-library `tomllib`). No network, no AI, no telemetry. Unix-like
systems only.

See a real generated report: [examples/sample-pr.md](https://github.com/harihkk/Debug-Brief/blob/main/examples/sample-pr.md).

## Install

DebugBrief is a Python CLI; the simplest installs put it on your PATH in its own
isolated environment:

```bash
pipx install debugbrief
# or
uv tool install debugbrief
```

Plain `pip install debugbrief` (or `pip install -e .` from a clone) works too.
DebugBrief itself needs Python 3.9+ and native `git` on a Unix-like system
(Linux and macOS are tested; BSD should work). Native Windows/PowerShell is not
supported. The project you debug does **not** need to be Python: only DebugBrief
runs on Python.

## Quickstart

```bash
debugbrief start "Fix add() returning wrong result"
debugbrief note "add() subtracts instead of adds; the test expects 5."
debugbrief run -- python -m pytest -q test_calc.py   # fails
# ... make your fix ...
debugbrief redo                                      # same test again: passes
debugbrief end                                       # writes the pr-style brief
```

Everything after `--` runs exactly as you typed it, with its output streaming
live to your terminal; DebugBrief flags (`--timeout`, `--shell`, `--no-redact`)
go before the `--`. Quoting the whole command also works: `debugbrief run
"pytest -q"`. `redo` re-runs the last captured command, and `end` defaults to
the `pr` report mode.

Tip: a one-line alias makes the capture prefix disappear in daily use:

```bash
alias db="debugbrief run --"
db pytest -q
```

`run` and `note` auto-start a session if you forget to, so a capture is never
lost (and `debugbrief cancel` discards a session you did not mean to start).
The resulting report leads with a derived one-liner like:

> Failing check `python -m pytest -q test_calc.py` passed after 2 attempts over
> 2s, changes touched calc.py.

## Works with any language

DebugBrief wraps whatever command you run, so the project being debugged can be
in any language. A recognized test/build/lint/typecheck runner is classified
automatically; any other command is still captured, and you mark it a check with
`--verify`:

```bash
debugbrief run -- npm test
debugbrief run -- go test ./...
debugbrief run -- cargo test
debugbrief run -- ./gradlew test          # or: mvn test
debugbrief run -- dotnet test
debugbrief run -- make check
debugbrief run --verify -- ./scripts/integration.sh   # custom check
```

A compound shell command (`run --shell "a && b | c"`) is recorded conservatively
as a single command, not attributed to one tool; run a check on its own, or
declare the whole command with `--verify`, to have it counted as a verification.

## How it works

- `run` executes a command under a pseudo-terminal so its output streams live,
  records its real exit code, bounded output, duration, and a per-command git
  snapshot, then returns the command's own exit code.
- Pass/fail comes only from the exit code. A command counts as "verified" only if
  a recognized test/build/lint/typecheck command actually exited `0`.
- Recognized runners include pytest, unittest, tox, vitest, jest, bun test,
  deno test, node --test, npm/pnpm/yarn test, go test, cargo test, make
  test/check, dotnet test, ctest, phpunit, mix test, swift test, rspec, and
  mvn/gradle test. For custom scripts, declare the check yourself:
  `debugbrief run --verify -- ./scripts/test.sh`.
- `end` derives the report from those events: the red-to-green window, the
  reproduce/verify commands, a timeline, the observed error verbatim, and the
  failed attempts. Empty sections are omitted, never padded.
- Redaction runs before anything reaches disk and catches common shapes:
  sensitive `name=value` pairs, bearer and authorization headers, OpenAI/AWS/
  GitHub style keys, connection-string passwords, and PEM private key blocks,
  each replaced with `[redacted]`. Best effort by design; `--no-redact` opts
  out per command.

## Commands

| Command | What it does |
| --- | --- |
| `init` | Set up the project and show the workflow |
| `start "<title>"` | Start a session |
| `note <text ...>` | Record a note (quoting optional) |
| `run -- <command ...>` | Execute and capture a command |
| `redo` | Re-run the last captured command |
| `preview [--mode ...]` | Print the report without ending the session |
| `end [--mode pr\|handoff\|incident] [--detail compact]` | Finalize and write a report (default `pr`) |
| `cancel [--yes]` | Discard the active session, no report |
| `status` | Show the active session |
| `doctor [--fix]` | Health-check the project and state |
| `recover` | Repair a broken session pointer after a crash |
| `last` / `open` | Find or open the most recent report |
| `list` / `show <id>` | Browse recorded sessions |

`debugbrief init` sets up a project in one step (and prints the `db` alias).
`end --detail compact` writes a shorter PR brief, and an optional
`.debugbrief.toml` sets default mode, timeout, and detail. Full detail, flags,
and the report modes: [docs/COMMANDS.md](https://github.com/harihkk/Debug-Brief/blob/main/docs/COMMANDS.md).

Post a brief straight to a PR (GitHub CLI optional):

```bash
debugbrief end --stdout | gh pr comment --body-file -
```

## Limitations

- Unix-like only; no Windows/PowerShell.
- Capture is explicit via `debugbrief run`. Output streams live while a bounded
  preview is stored for the report; DebugBrief does not keep a full transcript.
- Commands run under a pseudo-terminal so output streams live; full-screen TUIs
  (a `vim` session, `htop`) still are not meaningfully captured, since their
  cursor-control output is not linear text. Run those directly and record the
  outcome with `note`. Where no pseudo-terminal is available (a locked-down
  sandbox), capture falls back to plain pipes.
- Termination signals the command's process group, so ordinary background
  children are cleaned up. A child that detaches into its own session (`setsid`)
  but keeps one of the captured streams open is reported as a warning; one that
  also closes its inherited output descriptors can outlive the command without
  being detectable by a standard-library process-group runner.
- Redaction is conservative and best effort; it does not catch every secret.
- Git sections need native `git`; outside a repo they are omitted honestly.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT. See [LICENSE](https://github.com/harihkk/Debug-Brief/blob/main/LICENSE).
