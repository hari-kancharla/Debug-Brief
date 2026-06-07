# DebugBrief

[![CI](https://github.com/harihkk/DebugBrief/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/harihkk/DebugBrief/actions/workflows/ci.yml)

DebugBrief is a **local-first command-line tool** that records the meaningful
context of a debugging session and turns it into a useful, honest markdown
brief.

It captures the useful path of a debugging session (your notes, the commands
you ran, their real outcomes, your Git state changes, and verification steps)
and renders a concise report you can drop into a pull request, a handoff doc,
or an incident timeline.

**Supported Python:** 3.9+ (CI runs 3.10, 3.11, and 3.12). Unix-like systems
only (Linux, macOS, BSD).

## What DebugBrief is

- A way to produce **PR-ready debugging summaries**.
- A way to write **engineering handoff notes** for a tricky, half-solved issue.
- A way to capture an **incident / debug timeline**.
- A record of **what was tried, what changed, what failed, what passed, and how
  the fix was verified.**

## What DebugBrief is **not**

- It is **not** a journaling app.
- It is **not** a terminal recorder or full transcript capture.
- It is **not** an AI assistant. It does **not** call any model.
- It is **not** a cloud tool. There is no telemetry and no network usage.
- It is **not** a background daemon, a TUI, or a web app.
- It is **not** a shell integration. It does not modify your dotfiles or
  global shell config.

## Why it exists

The most valuable part of debugging is what you tried, what you ruled out, and
how you confirmed the fix. That context is usually gone the moment the bug is
closed. DebugBrief makes it cheap to record while you work and easy to turn into
a brief when you are done. Every line in a report is backed by something you
actually recorded. DebugBrief never invents a root cause, never fakes a test
result, and never claims verification that did not happen.

## Honesty guarantees

- Pass/fail comes **only** from the real process exit code. Exit code `0` means
  passed; anything else means failed.
- A command is "verified" only if a recognized test/build/lint/typecheck command
  actually exited `0`.
- Failed commands are never hidden.
- Command output is stored as a bounded **preview** (default 4000 characters per
  stream) and is explicitly flagged when truncated.
- Reports never fabricate intent, summaries, or root causes. Notes you wrote are
  the strongest signal; everything else is derived deterministically from
  recorded evidence.

## Requirements and supported platforms

- **Python 3.9+**.
- **Unix-like systems only** (Linux, macOS, BSD). Windows / PowerShell is **not**
  supported in v1; DebugBrief will exit with a clear error there.
- Native **`git`** on your `PATH` (optional: DebugBrief works outside a repo,
  it just won't capture Git state).

### Runtime dependencies

**None.** DebugBrief uses only the Python standard library and shells out to the
native `git` executable. `pytest` is used only for development/testing and is an
optional extra.

## Installation

From the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `debugbrief` command via `[project.scripts]` in
`pyproject.toml`:

```toml
[project.scripts]
debugbrief = "debugbrief.cli:main"
```

For development (tests):

```bash
pip install -e ".[dev]"
pytest
```

## Local storage

All state is stored locally inside the **project root**:

- If you are inside a Git repository, the project root is the **Git repo root**.
- Otherwise, it is the **current working directory** (DebugBrief continues
  safely without Git).

Layout:

```
.debugbrief/
  active_session.json            # pointer to the currently-active session
  sessions/
    <session_id>.json            # the full, canonical session record
  reports/
    <session_id>-pr.md
    <session_id>-handoff.md
    <session_id>-incident.md
```

When inside a Git repo, DebugBrief adds `.debugbrief/` to
`.git/info/exclude` (a **local**, untracked ignore file). It does **not** modify
your shared `.gitignore` and does **not** touch any other tracked files. If
`.git/info/exclude` cannot be written (for example in a worktree or submodule),
DebugBrief continues and records a warning instead of failing.

## Commands

```text
debugbrief start "<session title>"        Start a session
debugbrief note  "<note text>"            Record a note
debugbrief run   "<command>"              Execute and capture a command
debugbrief end   --mode pr|handoff|incident   Finalize + write a report
debugbrief status                         Show the active session
debugbrief doctor [--fix]                 Health-check the project + local state
debugbrief last                           Show the most recent report
debugbrief open  [--last | --path PATH]   Open a report in $EDITOR
debugbrief list  [--json]                 List recorded sessions
debugbrief show  <session_id> [--json]    Show one session summary
```

### `debugbrief run` details

`run` is the **primary, reliable capture mechanism**. It:

- requires an active session,
- executes the command from the project root,
- captures the command text, start/end timestamps, duration, exit code, and
  bounded stdout/stderr previews,
- classifies whether the command is a test and whether it is verification-worthy,
- marks pass/fail strictly from the exit code,
- persists the event immediately, and
- **returns the same exit code** as the executed command.

By default, the command is parsed with `shlex.split` and run **without** a
shell. To use shell features (pipes, redirection, `&&`), pass `--shell`:

```bash
debugbrief run --shell "pytest -q | tee out.txt"
```

`--shell` is explicit on purpose, because it has different parsing and safety
behavior.
The exact command string you typed is always preserved verbatim in the session
record.

#### Timeouts

The default command timeout is **300 seconds**. Override it with `--timeout`:

```bash
debugbrief run --timeout 600 "pytest tests/"
```

If a command times out, DebugBrief terminates it, records the event with status
`timed_out` and a `null` exit code, and returns a nonzero exit code.

### `debugbrief doctor`

`doctor` runs a read-only health check and prints `PASS` / `WARN` / `FAIL`
lines, then an overall verdict and a matching exit code:

- `0`: DebugBrief is ready (all checks pass)
- `1`: usable with warnings (at least one `WARN`, no `FAIL`)
- `2`: blocking issues (at least one `FAIL`)

It checks the platform, Python version, project root, Git repo / branch (or
detached HEAD), whether `.debugbrief/` exists and is writable, whether
`.debugbrief/` is in `.git/info/exclude`, the state and integrity of any active
session (valid JSON, points to the current project, not interrupted), the
reports directory, and that experimental shell mode is unavailable by design.

By default `doctor` does not mutate state. The optional `--fix` applies only
**safe** changes: it creates the `.debugbrief/` directories and adds
`.debugbrief/` to `.git/info/exclude`. It never touches `.gitignore`, global
config, or dotfiles.

```bash
debugbrief doctor
debugbrief doctor --fix
```

### `debugbrief last`

Prints the most recently generated report's path, its mode (inferred from the
filename), its modified time, and the first title line from the markdown. It
does not require an active session and does not open the file. If no reports
exist, it prints a clear message and exits nonzero.

```bash
debugbrief last
```

### `debugbrief open`

Opens a report in your `$EDITOR`. With no arguments (or `--last`) it opens the
latest report; `--path PATH` opens a specific report. It does not require an
active session.

- If `$EDITOR` is set, DebugBrief launches it (e.g. `vim`, or `code -w`).
- If `$EDITOR` is not set, DebugBrief prints the report path and a helpful hint
  instead of guessing a GUI command.
- If opening fails, it prints the path and exits nonzero.

```bash
debugbrief open
debugbrief open --last
debugbrief open --path .debugbrief/reports/<session_id>-pr.md
```

### `debugbrief list`

Lists recorded sessions, most recent first. It does not require an active
session and does not dump raw JSON by default. For each session it shows the
short id, status, title, start time, command count (and failures), notes count,
verification status, and which report modes have been generated. If no sessions
exist, it prints a clear message and exits nonzero. Pass `--json` for a
structured summary suitable for scripting.

```bash
debugbrief list
debugbrief list --json
```

### `debugbrief show`

Shows a compact, human-readable summary of one session: title, status, project
root, start and end time, notes, relevant commands, failed commands,
verification commands, changed files, and generated report paths. The id may be
a full session id or any unambiguous short prefix. Ambiguous or missing ids
produce a clear error. Pass `--json` to print the full structured session.

```bash
debugbrief show 3f8a1c2d
debugbrief show 3f8a1c2d --json
```

You can also run any command without installing the console script:

```bash
python -m debugbrief list
```

## Improved Git summary

When inside a Git repository, reports include a concise **Modified files**
section derived from `git status --porcelain`, with name-status labels:

```text
- `M` modified: `src/auth/refresh.py`
- `A` added: `tests/test_auth.py`
- `D` deleted: `docs/old.md`
- `R` renamed: `pkg/new_name.py`
```

This is built only from native `git` (no GitPython) and includes changed files,
name-status labels, shortstat line counts, the initial and final SHAs, and the
branch or detached-HEAD state. DebugBrief never includes a full diff or file
contents. Outside a Git repository, reports state plainly that Git metadata is
unavailable.

## Snapshot-tested reports

Report rendering is covered by **snapshot tests** (`tests/test_snapshots.py`)
for all three modes, stored under `tests/snapshots/`. Reports are generated from
deterministic, in-memory fake session data, and dynamic fields (UUIDs,
timestamps, Git SHAs, and absolute paths) are normalized to stable placeholders
before comparison, so the snapshots are meaningful but not brittle across
machines. To regenerate them on purpose:

```bash
DEBUGBRIEF_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py
```

The committed snapshots under `tests/snapshots/` (`pr_report.md`,
`handoff_report.md`, `incident_report.md`) double as concrete sample reports you
can read to see exactly what DebugBrief generates.

## Example session workflow

```bash
debugbrief doctor
debugbrief start "Fix auth token refresh race"
debugbrief note "Token refresh fails when two requests retry at the same time."
debugbrief run "python -m pytest tests/test_auth.py"
debugbrief run "python -m pytest tests/test_auth.py"
debugbrief end --mode pr
debugbrief last
debugbrief open
```

DebugBrief prints the path to the generated report, for example:

```
Session completed: Fix auth token refresh race
  mode:      pr
  report:    /path/to/repo/.debugbrief/reports/<session_id>-pr.md
  session:   /path/to/repo/.debugbrief/sessions/<session_id>.json
```

### Example report output (PR mode, abridged)

```markdown
# Fix auth token refresh race

## Session metadata
- **Status:** COMPLETED
- **Git branch:** feature/auth
- **Notes:** 2  **Commands:** 3  **Failed commands:** 1

## Overview
This pull request summarizes the debugging session "Fix auth token refresh
race". 2 file(s) were changed (+12 / -4). 3 command(s) were recorded, 1 of
which failed. Verification: at least one verification command passed.

## Key findings
- Token refresh fails when two requests retry at the same time.
- Failure points to refresh state being shared across concurrent requests.

## Verification and tests
- [passed] test (pytest): `python -m pytest tests/test_auth.py`

## Risks / follow-up
- No outstanding risks were detected automatically. Review manually.
```

Every section is built only from what you recorded. If you ran no tests, the
report says so. If nothing changed, it says that plainly. It will not pretend.

## Report modes

- **`pr`** for pull-request-ready output: overview, key findings, changes,
  modified files, verification, relevant commands, risks/follow-up.
- **`handoff`** for handing a tricky issue to someone else: current status,
  working hypotheses, a timeline of meaningful steps, commands attempted, files
  touched, current repo state, and suggested next steps.
- **`incident`** for a chronological engineering note: executive summary, time
  window, event timeline, actions taken, resolution/current state, verification,
  and follow-ups.

## Recovery behavior for interrupted sessions

The canonical record for an active session is its file under
`.debugbrief/sessions/<id>.json`, rewritten immediately after every note and
command, so a crash never loses captured work. `active_session.json` is a small
pointer that is removed only on a clean `end`.

If a session looks interrupted or inconsistent (for example the pointer exists
but its session file is missing), `debugbrief status` reports it and prints
recovery steps:

```text
A session appears INTERRUPTED or inconsistent.
Recovery:
  - Inspect .debugbrief/active_session.json and the sessions/ folder.
  - Remove .debugbrief/active_session.json to clear the active pointer,
    then start a fresh session.
```

## Experimental shell mode (`start --shell`)

Capturing arbitrary shell history reliably across shells and platforms is hard
and easy to get subtly wrong. Rather than ship something that silently produces
incomplete or fabricated history, **v1 does not implement `start --shell`.**
Running it returns a clear message pointing you to the reliable model:

```text
Experimental shell-history capture (start --shell) is not available in v1.
The reliable capture model is explicit execution:
  debugbrief run "<command>"
```

This is a deliberate choice. The core tool is fully useful through
`debugbrief run`, and stating a limitation plainly beats pretending to capture
something it cannot.

> Note: `start --shell` (experimental history capture, not available) is
> different from `run --shell` (run a single command through the system shell,
> fully supported).

## Limitations

- Unix-like systems only; no Windows/PowerShell support in v1.
- No full terminal transcript capture and no PTY tricks. Capture is explicit via
  `debugbrief run`.
- Command output is stored as bounded previews, not complete logs.
- Git state is captured via the native `git` CLI; outside a repo, Git-derived
  sections are omitted honestly.
- `.git/info/exclude` updates may be unavailable in worktrees/submodules; this is
  reported as a warning, not a failure.

## No AI, no invented summaries

DebugBrief contains no AI and makes no network calls. All summaries are produced
by deterministic heuristics over data you explicitly recorded. It will never
invent a root cause, a command, a test result, or a verification claim.

## License

MIT.
