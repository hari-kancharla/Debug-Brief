# DebugBrief

[![CI](https://github.com/harihkk/Debug-Brief/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/harihkk/Debug-Brief/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/debugbrief?cacheSeconds=3600)](https://pypi.org/project/debugbrief/)

Turn a debugging session into an honest markdown brief for a PR, a handoff, or an
incident note.

You record notes and run commands through DebugBrief while you work. When you are
done, it writes a report built only from what actually happened: what you tried,
what failed, what then passed, and which files changed in between. It never
invents a root cause and never claims a test result you did not get.

It is local-first and dependency-free: standard library plus native `git`, no
network, no AI, no telemetry. Unix-like systems only.

See a real generated report: [examples/sample-pr.md](examples/sample-pr.md).

## Install

```bash
pip install debugbrief
```

Or from a clone: `pip install -e .`. Needs Python 3.9+ and native `git` on a
Unix-like system (Linux, macOS, BSD).

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

## How it works

- `run` executes a command, records its real exit code, bounded output, duration,
  and a per-command git snapshot, then returns the command's own exit code.
- Pass/fail comes only from the exit code. A command counts as "verified" only if
  a recognized test/build/lint/typecheck command actually exited `0`.
- `end` derives the report from those events: the red-to-green window, the
  reproduce/verify commands, a timeline, the observed error verbatim, and what
  was ruled out. Empty sections are omitted, never padded.
- Secret-like values in captured output are replaced with `[redacted]` before
  anything is written to disk (best effort; `--no-redact` opts out).

## Commands

| Command | What it does |
| --- | --- |
| `start "<title>"` | Start a session |
| `note <text ...>` | Record a note (quoting optional) |
| `run -- <command ...>` | Execute and capture a command |
| `redo` | Re-run the last captured command |
| `end [--mode pr\|handoff\|incident]` | Finalize and write a report (default `pr`) |
| `cancel [--yes]` | Discard the active session, no report |
| `status` | Show the active session |
| `doctor [--fix]` | Health-check the project and state |
| `last` / `open` | Find or open the most recent report |
| `list` / `show <id>` | Browse recorded sessions |

Full detail, flags, and the report modes: [docs/COMMANDS.md](docs/COMMANDS.md).

Post a brief straight to a PR (GitHub CLI optional):

```bash
debugbrief end --stdout | gh pr comment --body-file -
```

## Limitations

- Unix-like only; no Windows/PowerShell.
- Capture is explicit via `debugbrief run`. There is no terminal transcript or
  PTY capture, and output is stored as bounded previews, not full logs.
- Interactive and TUI commands (a pdb session, watch modes) will behave oddly
  under `run`, because output is piped for capture rather than attached to a
  terminal. Run those directly and record the outcome with `note`.
- Redaction is conservative and best effort; it does not catch every secret.
- Git sections need native `git`; outside a repo they are omitted honestly.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT. See [LICENSE](LICENSE).
