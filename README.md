# DebugBrief

[![CI](https://github.com/harihkk/Debug-Brief/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/harihkk/Debug-Brief/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/debugbrief?cacheSeconds=3600)](https://pypi.org/project/debugbrief/)

Record what you do while debugging and turn it into an evidence-backed report for a pull request, a handoff, or an incident note.

![A failing test streams live, the fix lands, redo passes, and the generated report appears](https://raw.githubusercontent.com/harihkk/Debug-Brief/main/docs/demo.gif)

DebugBrief captures the notes you write and the commands you run, then builds a report from what actually happened: what you tried, what failed, what passed, and which files changed in between. It does not use AI and does not infer a root cause or report a test result you did not get.

The product site is a static, dependency-free walkthrough of the workflow: [open the site source](https://github.com/harihkk/Debug-Brief/tree/main/site), or serve `site/index.html` locally. It is included in the source distribution and is intentionally repository/deployment content rather than runtime wheel data.

## Install

```bash
pipx install debugbrief
# or
uv tool install debugbrief
```

Plain `pip install debugbrief` works too. DebugBrief needs Python 3.9 or newer. Native Git is used when available to capture repository metadata and changed files. The project you debug does not need to be Python; only DebugBrief runs on Python.

## Quickstart

```bash
debugbrief run -- pytest -q test_calc.py             # auto-starts a local session
# make the fix
debugbrief redo                                      # same test, now passes
debugbrief end --format html --open                 # review the proof in your browser
```

A session title and notes are optional; add them when the output tells you something worth keeping. Everything after `--` runs exactly as you typed it, with its output streaming live to your terminal. DebugBrief flags (`--timeout`, `--shell`, `--no-redact`, `--verify`) go before the `--`. `redo` re-runs the last captured command; use `redo --dry-run` to preview it without a lease or execution. Conservative destructive redos ask for confirmation, and `--yes` bypasses that prompt. `end` defaults to the `pr` report mode.

Tip: a one-line alias makes the capture prefix disappear in daily use.

```bash
alias db="debugbrief run --"
db pytest -q
```

## What you get

A report built only from recorded evidence. A short excerpt from a real run:

```markdown
## Summary

Failing check `python -m pytest -q test_calc.py` passed after 2 attempts over 2s, changes touched calc.py.

## Red to green

A check failed at 12:02:09 and `python -m pytest -q test_calc.py` passed at 12:02:10 (window 1s).

Between the failing and passing checks, these files changed (correlation, not proven cause):
- `calc.py`
```

Full samples: [PR](https://github.com/harihkk/Debug-Brief/blob/main/examples/sample-pr.md), [handoff](https://github.com/harihkk/Debug-Brief/blob/main/examples/sample-handoff.md), [incident](https://github.com/harihkk/Debug-Brief/blob/main/examples/sample-incident.md).

## How it works

- `run` executes a command under a pseudo-terminal so its output streams live, then records the real exit code, a bounded output preview, the duration, and a per-command `git` snapshot when the project is a repository.
- Pass or fail comes only from the exit code. A command counts as verified when a recognized test, build, lint, or typecheck runner exits 0, or when you explicitly declare another command with `--verify` and its exit status is trustworthy.
- It works with any language. A recognized runner (pytest, jest, vitest, go test, cargo test, dotnet test, make check, and more) is classified automatically. Any other command is still captured, and you mark it a check with `--verify`.
- `end` derives the report from those events: an explicit evidence state, the red-to-green window, the reproduce and verify commands, a timeline, the observed error, and the failed attempts. PR reports are compact by default; pass `--detail full` for the complete record. Empty sections are omitted.
- `end --format html` writes a self-contained browser view; `end --format all` writes Markdown, JSON, and HTML together. Review it with `debugbrief open --html`, or use `debugbrief last --open` for a one-step handoff.
- Generated reports show only the local repository basename by default; pass `--include-full-path` to `end` or `preview` when the absolute project root is intentional.
- Secrets are redacted before anything is written to disk. Redaction is best effort; `--no-redact` opts out for a single command.

Full command reference and the complete recognized-runner list: [docs/COMMANDS.md](https://github.com/harihkk/Debug-Brief/blob/main/docs/COMMANDS.md). Security model and redaction details: [SECURITY.md](https://github.com/harihkk/Debug-Brief/blob/main/SECURITY.md).

Post a report straight to a pull request (GitHub CLI optional):

```bash
debugbrief end --stdout | gh pr comment --body-file -
```

## Commands

The six you use most:

| Command | What it does |
| --- | --- |
| `start "<title>"` | Start a session |
| `note <text>` | Record an observation |
| `run -- <command>` | Run and capture a command |
| `redo [--dry-run] [--yes]` | Re-run the last captured command, with a safe preview/confirmation gate |
| `end [--mode pr\|handoff\|incident] [--format md\|json\|html\|both\|all]` | Finalize and write a report (default `pr`, Markdown) |
| `open --html` / `last --open` | Review the saved proof in a browser or editor |

The rest:

| Command | What it does |
| --- | --- |
| `init` | Set up the project and show the workflow |
| `status` | Show the active session |
| `preview [--mode ...] [--format md\|json\|html]` | Print a report preview without ending the session |
| `cancel [--yes]` | Abandon the active session; no report, local record retained |
| `doctor [--fix]` | Health-check the project and state |
| `recover` | Repair a broken session pointer after a crash |
| `last` | Show the most recent report path |
| `open` | Open the most recent report |
| `list` | List recorded sessions |
| `show <id>` | Show a recorded session |

Full flags and behavior for each: [docs/COMMANDS.md](https://github.com/harihkk/Debug-Brief/blob/main/docs/COMMANDS.md).

## Dependencies

DebugBrief uses the Python standard library and native `git`. On Python 3.11 and newer it needs nothing else. On Python 3.9 and 3.10 it uses the small `tomli` package to read an optional `.debugbrief.toml`. DebugBrief itself makes no network requests, uses no AI, and collects no telemetry.

## Supported platforms

Linux and macOS are tested in CI across Python 3.9 through 3.14. Other Unix-like systems may work but are not currently tested. Native Windows and PowerShell are not supported.

## Limitations

- Capture is explicit through `debugbrief run`. Output streams live while a bounded preview is stored for the report, so there is no full transcript.
- Full-screen TUIs (a `vim` session, `htop`) are not meaningfully captured. Run those directly and record the outcome with `note`.
- Redaction is conservative and best effort; it does not catch every secret.
- Git sections need native `git`; outside a repository they are omitted.

## Development

```bash
pip install -e ".[dev]"
pytest
```

See [CONTRIBUTING.md](https://github.com/harihkk/Debug-Brief/blob/main/CONTRIBUTING.md) for the full setup and contribution guidelines.

## License

MIT. See [LICENSE](https://github.com/harihkk/Debug-Brief/blob/main/LICENSE).
