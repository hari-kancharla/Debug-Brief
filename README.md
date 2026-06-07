# DebugBrief

[![CI](https://github.com/harihkk/Debug-Brief/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/harihkk/Debug-Brief/actions/workflows/ci.yml)

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
pip install -e .
```

## Quickstart

```bash
debugbrief start "Fix add() returning wrong result"
debugbrief note "add() subtracts instead of adds; the test expects 5."
debugbrief run "python -m pytest -q test_calc.py"   # fails
# ... make your fix ...
debugbrief run "python -m pytest -q test_calc.py"   # passes
debugbrief end --mode pr
```

`run` and `note` auto-start a session if you forget to, so a capture is never
lost. The resulting report leads with a derived one-liner like:

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
| `note "<text>"` | Record a note |
| `run "<command>"` | Execute and capture a command |
| `end --mode pr\|handoff\|incident` | Finalize and write a report |
| `status` | Show the active session |
| `doctor [--fix]` | Health-check the project and state |
| `last` / `open` | Find or open the most recent report |
| `list` / `show <id>` | Browse recorded sessions |

Full detail, flags, and the report modes: [docs/COMMANDS.md](docs/COMMANDS.md).

Post a brief straight to a PR (GitHub CLI optional):

```bash
gh pr comment --body-file "$(ls -t .debugbrief/reports/*-pr.md | head -1)"
```

## Limitations

- Unix-like only; no Windows/PowerShell.
- Capture is explicit via `debugbrief run`. There is no terminal transcript or
  PTY capture, and output is stored as bounded previews, not full logs.
- Redaction is conservative and best effort; it does not catch every secret.
- Git sections need native `git`; outside a repo they are omitted honestly.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT. See [LICENSE](LICENSE).
