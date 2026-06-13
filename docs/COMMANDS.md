# DebugBrief commands

Full reference for every command. The README has the short version; this is the
detail.

```text
debugbrief init                               Set up the project and show the workflow
debugbrief start "<session title>"            Start a session
debugbrief note  <text ...>                   Record a note (quoting optional)
debugbrief run   -- <command ...>             Execute and capture a command
debugbrief redo                               Re-run the last captured command
debugbrief preview [--mode pr|handoff|incident] Print the report without ending
debugbrief end   [--mode pr|handoff|incident] Finalize and write a report
debugbrief cancel [--yes]                     Discard the active session
debugbrief status                             Show the active session
debugbrief doctor [--fix]                     Health-check the project and state
debugbrief last                               Show the most recent report
debugbrief open  [--last | --path PATH]       Open a report in $EDITOR
debugbrief list  [--json]                     List recorded sessions
debugbrief show  <session_id> [--json]        Show one session summary
```

`run` and `note` auto-start a session if none is active (see below).

## init

One-time onboarding for a project. It performs the same safe setup as
`doctor --fix` (creates `.debugbrief/` and adds it to `.git/info/exclude`),
reports health, and prints the recommended `db` alias and the daily workflow.

```bash
debugbrief init
```

It never starts a session or writes a report, so it is safe to run at any time.
The suggested alias makes the capture prefix disappear:

```bash
alias db="debugbrief run --"
db pytest -q
```

## start

Starts a session and records the initial Git state.

```bash
debugbrief start "Fix auth token refresh race"
```

When inside a Git repo, `start` also adds `.debugbrief/` to `.git/info/exclude`
(a local, untracked ignore file). It never touches a shared `.gitignore`. If the
exclude file cannot be written (for example in a worktree or submodule), it
records a warning and continues.

`start --shell` (experimental shell-history capture) is intentionally not
implemented; see [Experimental shell mode](#experimental-shell-mode).

## note

Records a note on the active session. Quoting is optional: multiple tokens are
joined with single spaces, so plain prose just works. Quote the note when it
contains characters your shell would interpret (quotes, parentheses, `;`, `|`).

```bash
debugbrief note remember to check the lock ordering
debugbrief note "Token refresh fails when two requests retry at once."
```

A note that begins with a dash would be read as a flag, so quote it or put
`--` first:

```bash
debugbrief note -- --force was the wrong call here
```

If no session is active, `note` auto-starts one first.

## run

The primary, reliable capture mechanism. Put DebugBrief's own flags first, then
`--`, then the command exactly as you would normally type it (no quoting
needed):

```bash
debugbrief run -- python -m pytest -q tests/
debugbrief run --timeout 600 -- make build
```

The old single-argument form still works: `debugbrief run "python -m pytest"`.

`run`:

- executes the command from the project root,
- streams the command's stdout and stderr to your terminal live (it runs under
  a pseudo-terminal), while accumulating them for the stored previews
  (DebugBrief's own status lines go to stderr, so the command's stdout stays
  clean for piping; see "Live output" below),
- captures the command text, start/end timestamps, duration, exit code, and
  bounded stdout/stderr previews,
- records a lightweight per-command Git snapshot (HEAD and changed files) so the
  report can correlate file changes with what happened,
- classifies whether the command is a test or a verification command,
- marks pass/fail strictly from the exit code,
- redacts secret-like values before writing anything to disk (see below),
- persists the event immediately, and
- returns the same exit code as the executed command.

By default the command is parsed with `shlex.split` and run without a shell. For
pipes, redirection, or `&&`, pass `--shell` with the command as one quoted
string:

```bash
debugbrief run --shell "pytest -q | tee out.txt"
```

If no session is active, `run` auto-starts one first.

### Live output

The command runs under a pseudo-terminal, which gives it terminal-like
buffering, so it streams its output live. This matters because most programs
decide how to buffer by asking whether their output is a terminal: behind a
plain pipe they block-buffer and nothing appears until they exit. The pty makes
them see a terminal, so even a plain `python script.py` prints line by line as
it runs. The output streams live while a bounded preview is stored for the
report.

Two small consequences, both handled:

- A program that adds color on a terminal will do so here; the live output keeps
  the color, while the stored report has the color codes stripped so it stays
  readable.
- In a locked-down sandbox where no pseudo-terminal can be allocated, DebugBrief
  falls back to plain pipes. Capture still works; only the live buffering
  behavior reverts.

### Declaring custom checks with --verify

Recognized test runners (pytest, vitest, bun test, deno test, node --test, go
test, cargo test, jest, npm/pnpm/yarn test, make test/check, tox, unittest,
dotnet test, ctest, phpunit, mix test, swift test, rspec, mvn/gradle test) are
classified automatically. For everything else, `--verify` declares the command
a check:

```bash
debugbrief run --verify -- ./scripts/integration.sh
```

A declared check counts as verification only when it actually exits 0; a
failing one is recorded as a failed check, which is exactly what feeds the
reproduce line and the red-to-green window. On a recognized runner the flag is
a no-op: the automatic classification wins.

### Timeouts, interrupts, and background processes

The command runs in its own process group, so termination reaches the group,
not just the immediate process.

The default timeout is 300 seconds. Override with `--timeout`:

```bash
debugbrief run --timeout 600 -- pytest tests/
```

On timeout the process group is terminated (SIGTERM then SIGKILL), the event is
recorded with status `timed_out` and a `null` exit code, and a nonzero code is
returned. Ordinary background children are cleaned up; a child that detaches
into its own session (`setsid`) can still outlive the command, which is reported
as a warning.

Ctrl-C terminates the group the same way and records the command with an
`interrupted` status, so an aborted attempt still appears in the report. The
event stores the raw code the child died with (a negative signal number), while
the CLI exits 130 because you interrupted the run. A command killed by a signal
otherwise reports the conventional `128 + N` exit code (SIGINT is 130).

If the command exits but a background process it started keeps the output stream
open (for example `sh -c 'devserver &'`), `run` drains what is buffered and
returns with a warning rather than blocking on the open stream. The retained
preview is bounded in memory regardless of how much the command prints.

### Redaction

Captured stdout/stderr previews and the command text are passed through a
conservative, best-effort secret scrubber before they are written. Recognized
shapes (provider keys, bearer/authorization values, private key blocks,
`scheme://user:password@host` connection strings, and `name=value` pairs whose
name looks sensitive) are replaced with `[redacted]`. Redaction is on by
default. It is best effort and does not catch everything.

To store raw output verbatim (only when you know it is safe):

```bash
debugbrief run --no-redact -- printenv MY_PUBLIC_VALUE
```

When a report includes output that had something masked, it says so.

## redo

Re-runs the most recently captured command in the active session, recording the
result as a new command event. This is the core debugging loop: run the test,
edit, run the same test again.

```bash
debugbrief run -- python -m pytest -q tests/test_auth.py   # fails
# ... edit ...
debugbrief redo                                            # same test again
```

`redo` re-executes the exact stored command with the same shell mode it was
originally run with, streams its output live, and returns the command's own
exit code, just like `run`. It accepts `--timeout`, `--no-redact`, and
`--verify`. A command originally declared with `run --verify` stays a declared
check on redo automatically; no need to retype the flag.

If there is no active session, or the session has no captured commands yet,
`redo` says so and exits 1. If the stored command itself had a secret masked
(it contains the `[redacted]` placeholder), `redo` refuses to re-run it, since
the placeholder is not the real command.

## preview

Prints the report for the active session to stdout without ending the session
or writing any file. Useful mid-session to check whether you have captured
enough before finalizing. `--mode` works as on `end` (default `pr`).

```bash
debugbrief preview
debugbrief preview --mode handoff
```

The output carries a banner line marking it as a preview of an active session.
The session is untouched: same status, same file, no report written.

## end

Finalizes the session, captures final Git state, and writes a report.
`--mode` defaults to `pr`.

```bash
debugbrief end                  # pr-style report
debugbrief end --mode handoff
debugbrief end --mode incident
```

Choose the output format with `--format` (default `md`):

```bash
debugbrief end --format both   # writes both markdown and JSON
debugbrief end --format json   # JSON only
```

The JSON report carries the same derived fields as the markdown and is written
next to it under `.debugbrief/reports/<id>-<mode>.json`.

`--stdout` prints the rendered markdown report to stdout while the files are
still written as usual; every informational line moves to stderr so the output
pipes cleanly. Posting a brief straight to a pull request becomes one line:

```bash
debugbrief end --stdout | gh pr comment --body-file -
```

### Report modes

- `pr`: pull-request-ready. One-line summary, reproduce/verify commands, the
  red-to-green window, modified files, a condensed timeline, verification, and
  what was ruled out.
- `handoff`: hand a tricky issue to someone else. Current status, your notes, the
  full timeline, commands attempted, what was ruled out, current repo state, and
  next steps drawn only from your recorded notes.
- `incident`: a chronological note. One-line summary, time window, full timeline,
  the observed error verbatim, resolution/current state, verification, and
  follow-ups.

Every section is rendered only when it has real content. Nothing is invented.

## cancel

Discards the active session without writing a report. The session file is kept
on disk with status `ABANDONED`, so nothing is silently deleted; it simply
never becomes a brief. Useful when auto-start kicked in on a command you did
not mean to capture.

```bash
debugbrief cancel        # asks: Discard active session '<title>'? [y/N]
debugbrief cancel --yes  # no prompt
```

Anything other than `y` (including no stdin at all) declines: the prompt aborts
safely, leaves the session active and untouched, and exits nonzero. Abandoned
sessions still show up in `list` and `show`.

## status

Shows the active session, or reports a clear recovery path if a session looks
interrupted (for example the active pointer exists but its session file is gone).

```bash
debugbrief status
```

## doctor

A read-only health check that prints `PASS` / `WARN` / `FAIL` lines and an
overall verdict with a matching exit code:

- `0`: ready (all checks pass)
- `1`: usable with warnings
- `2`: blocking issues

```bash
debugbrief doctor
debugbrief doctor --fix
```

`--fix` applies only safe changes: it creates the `.debugbrief/` directories and
adds `.debugbrief/` to `.git/info/exclude`. It never touches `.gitignore`,
global config, or dotfiles.

## last

Prints the most recent report's path, its mode, modified time, and title.

```bash
debugbrief last
```

## open

Opens a report in `$EDITOR` (latest by default, or `--path PATH`). If `$EDITOR`
is not set, it prints the path instead of guessing.

```bash
debugbrief open
debugbrief open --path .debugbrief/reports/<id>-pr.md
```

## list

Lists recorded sessions, most recent first. Pass `--json` for a structured
summary.

```bash
debugbrief list
debugbrief list --json
```

## show

Shows a compact summary of one session. The id may be a full id or any
unambiguous short prefix. Pass `--json` for the full structured session.

```bash
debugbrief show 3f8a1c2d
debugbrief show 3f8a1c2d --json
```

You can run any command without installing the console script:

```bash
python -m debugbrief list
```

## Post a brief to a pull request

The markdown report is plain text, so posting it as a PR comment with the GitHub
CLI is a one-liner (`gh` is optional and never required by DebugBrief):

```bash
debugbrief end --mode pr
gh pr comment --body-file "$(ls -t .debugbrief/reports/*-pr.md | head -1)"
```

## Local storage

All state lives under the project root:

```text
.debugbrief/
  active_session.json            # pointer to the active session
  sessions/<session_id>.json     # the full, canonical session record
  reports/<session_id>-<mode>.md # generated reports (and .json with --format)
```

The project root is the Git repo root when inside a repo, otherwise the current
working directory.

## Recovery for interrupted sessions

The canonical record is the session file under `.debugbrief/sessions/<id>.json`,
rewritten after every note and command, so a crash never loses captured work.
`active_session.json` is a small pointer removed only on a clean `end`. If the
pointer exists but the session file is missing, `status` reports it and prints
recovery steps.

## Experimental shell mode

Capturing arbitrary shell history reliably across shells and platforms is hard
and easy to get subtly wrong. Rather than ship something that silently produces
incomplete or fabricated history, `start --shell` is not implemented. Running it
prints a clear message pointing to the reliable model: explicit `debugbrief run`.

This is different from `run --shell`, which runs a single command through the
system shell and is fully supported.

## Snapshot-tested reports

Report rendering is covered by snapshot tests under `tests/snapshots/`. The
committed snapshots double as concrete sample reports. To regenerate them on
purpose:

```bash
DEBUGBRIEF_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py
```
