# DebugBrief commands

Reference for DebugBrief 1.4 and newer. The README covers the daily workflow;
this page describes flags, recorded evidence and recovery.

```text
debugbrief init                               Set up the project and show the workflow
debugbrief start "<session title>"            Start a session
debugbrief note  <text ...>                   Record a note (quoting optional)
debugbrief run   -- <command ...>             Execute and capture a command
debugbrief redo [--dry-run] [--yes]            Re-run the last captured command
debugbrief preview [--mode pr|handoff|incident] [--format md|json|html] Print without ending
debugbrief end   [--mode pr|handoff|incident] [--include-full-path] Finalize and write
debugbrief cancel [--yes]                     Abandon the active session
debugbrief status                             Show the active session
debugbrief doctor [--fix]                     Health-check the project and state
debugbrief recover                            Repair a broken/stale active pointer
debugbrief last [--open]                      Show or open the most recent report
debugbrief open  [--last | --path PATH] [--html] Open in $EDITOR or the system browser
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

Use `--` before a note that begins with a dash so it cannot be parsed as a flag:

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

- executes the command from the directory you run it in, not the project root
  (your DebugBrief state still lives at the project root),
- streams the command's stdout and stderr to your terminal live (it runs under
  a pseudo-terminal), while accumulating them for the stored previews
  (DebugBrief's own status lines go to stderr, so the command's stdout stays
  clean for piping; see "Live output" below),
- captures the command text, start/end timestamps, duration, exit code, and
  bounded stdout/stderr previews,
- records a lightweight per-command Git snapshot when the project is a repository
  (HEAD and changed files) so the report can correlate file changes with what happened,
- retains the working directory for replay and exports its project-relative location with command history and reproduction/verification instructions; older records without that location remain explicitly unknown,
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

DebugBrief runs `--shell` commands through bash (when available) with `pipefail`
set, so a pipeline exits nonzero if any stage fails, not only its last (pipefail
reports the rightmost failing stage's status, enough to tell pass from fail but
not which stage failed). A **compound** shell command (anything joined by `|`,
`&&`, `||`, `;`, `&`, or a newline) is recorded as a single command and is
**not** attributed to an internal tool: an exit code cannot say which stage
produced it. To get a recorded verification, run the check on its own
(`debugbrief run -- pytest`), or declare the whole command a check with
`--verify`, which is honored only when the exit code is reliable (for a pipeline,
that means pipefail is in effect).

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

Help, version, collection-only and test-listing invocations never qualify, even
with `--verify`. A report can be verified without a prior failure, but at least
one eligible check must pass, no check may currently fail, and every recorded
verification candidate must have a valid timestamp. An invalid timestamp in an
older attempt still prevents verification. This says nothing about checks that
were not recorded.

Build, lint and typecheck recognition includes `npm run build`, `pnpm build` and
`pnpm run build`, `yarn build`, the corresponding npm/pnpm/yarn lint and typecheck
scripts, `mypy`, `ruff check`, `black --check` and `tsc --noEmit`. Test recognition
also accepts `py.test`, package-manager `run test` forms and supported wrappers
such as `python -m`, `uv run`, `poetry run`, `npx` and `bundle exec`.

A red-to-green window requires a Git repository and the same command to fail and
later pass in the same recorded directory. It is historical evidence: a later
failure still makes the current report unverified. Changed files in the window
are a correlation, not an inferred root cause.

### Timeouts, interrupts, and background processes

The command runs in a process group owned by an independent supervisor, so
termination reaches ordinary descendants as well as the immediate process.

The default timeout is 300 seconds. Override with `--timeout`:

```bash
debugbrief run --timeout 600 -- pytest tests/
```

On timeout the process group is terminated (SIGTERM then SIGKILL), the event is
recorded with status `timed_out` and a `null` exit code, and a nonzero code is
returned. Ordinary background children are cleaned up; a child that detaches
into its own session (`setsid`) can still outlive the command, which is reported
as a warning when it retains a captured output stream. A detached child that
also closes its output cannot be detected this way.

If the recorder itself is killed, including with `SIGKILL`, the supervisor
detects its closed lifetime pipe and stops the owned process group. It allows
half a second for termination before escalating to `SIGKILL`. This works with
PTYs and the pipe fallback and does not depend on command output. A surviving
detached process remains outside this guarantee. The supervisor is reaped on
normal completion; ordinary background descendants are stopped then, too.

After cleanup settles, `debugbrief recover` releases stale session metadata and
preserves the recorded work. If called during cleanup it can still report the
command as running; retry after cleanup. It never clears a held command lock or
invents an exit code for a capture lost in a crash. The report remains explicit
about that missing outcome.

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
originally run with, streams its output live, and returns the command's own exit
code, just like `run`. It accepts `--timeout`, `--no-redact`, and
`--verify`. A command originally declared with `run --verify` stays a declared
check on redo automatically; no need to retype the flag.

Use `debugbrief redo --dry-run` to print the stored command and its original
working directory without acquiring a command lease or executing anything.
Conservative destructive commands (including removal, hard resets/cleans,
force pushes, destructive container/Kubernetes/Terraform operations, and
publish/deploy-style commands) require a `[y/N]` confirmation when standard
input is available. EOF, an unreadable input stream, or any answer other than
`y`/`yes` aborts safely. Use `debugbrief redo --yes` to bypass the confirmation;
ordinary commands such as `pytest` and `echo` remain frictionless.

If there is no active session, or the session has no captured commands yet,
`redo` says so and exits 1. If the stored command itself had a secret masked
(it contains the `[redacted]` placeholder), `redo` refuses to re-run it, since
the placeholder is not the real command.

`redo` is a fresh execution against the current files, environment and dependencies.
It does not recreate an earlier environment.

## preview

Prints the report for the active session to stdout without ending the session
or writing any file. Useful mid-session to check whether you have captured
enough before finalizing. `--mode` works as on `end` (default `pr`), and
`--format` accepts `md`, `json`, or `html`.

```bash
debugbrief preview
debugbrief preview --mode handoff
debugbrief preview --format html
```

Markdown previews carry a banner line marking the active session; JSON and HTML
previews stay machine-clean. The session is untouched: same status, same file, no
report written. Reports show only the local repository basename in project-root
metadata by default; pass `--include-full-path` when the absolute root is
intentional. Paths typed into commands, notes and output can still appear;
this flag controls the project-root field only.

## end

Finalizes the session, captures final Git state, and writes a report.
`--mode` defaults to `pr`.

```bash
debugbrief end                  # pr-style report
debugbrief end --mode handoff
debugbrief end --mode incident
debugbrief end --detail compact # shorter PR brief; metadata/timeline collapsed
```

PR Markdown defaults to `--detail compact`: summary, evidence state, changed files
and verification stay visible while metadata and timeline fold into a collapsible
section. Use `--detail full` for the full record. `preview` accepts the same flag.
Both commands hide the absolute project-root field by default; use
`--include-full-path` to include it deliberately.

Choose the output format with `--format` (default `md`):

```bash
debugbrief end --format both   # Markdown and JSON
debugbrief end --format html   # self-contained browser report
debugbrief end --format all    # Markdown, JSON, and HTML
debugbrief end --format html --open  # write and open the browser brief
```

The JSON report carries the same derived fields as the markdown, including
`line_stats`, and is written next to it under
`.debugbrief/reports/<id>-<mode>.json`. The HTML report is self-contained, has
no external assets, and is written as `.debugbrief/reports/<id>-<mode>.html`.
HTML is a complete, self-contained view; `--detail` controls the Markdown report.

Current JSON reports include per-attempt commands, exit codes, bounded output
previews, truncation and redaction flags, current failures and project-relative
working directories. Export a report with `--format json` for the website's
Evidence Explorer. `show --json` prints raw session state instead and is not an
Explorer report. The Explorer accepts one file up to 5 MiB and 2,000 entries per
list; missing fields in older supported reports remain explicitly unknown.

`--stdout` prints the report while still writing the requested files. It emits
HTML for `--format html`, JSON for `--format json`, and Markdown for `md`, `both`
or `all`. Informational lines go to stderr so the report pipes cleanly. Posting a Markdown brief straight to a pull request becomes one line:

```bash
debugbrief end --stdout | gh pr comment --body-file -
```

### Report modes

- `pr`: pull-request-ready. One-line summary, reproduce/verify commands, the
  red-to-green window, modified files, a condensed timeline, verification, and
  the failed attempts.
- `handoff`: hand a tricky issue to someone else. Current status, your notes, the
  full timeline, commands attempted, the failed attempts, current repo state, and
  next steps drawn only from your recorded notes.
- `incident`: a chronological note. One-line summary, time window, full timeline,
  the observed error excerpt, resolution/current state, verification, and
  follow-ups.

Every section is rendered only when it has real content. Nothing is invented.

## cancel

Abandons the active session without writing a report. The session file is kept
on disk with status `ABANDONED`, so nothing is silently deleted; it simply
never becomes a brief. Useful when auto-start kicked in on a command you did
not mean to capture.

```bash
debugbrief cancel        # asks: Abandon active session '<title>'? [y/N]
debugbrief cancel --yes  # no prompt
```

Anything other than `y` (including no stdin at all) declines: the prompt aborts
safely, leaves the session active and untouched, and exits nonzero. Abandoned
sessions still show up in `list` and `show`.

## status

Shows the active session, any currently running captured command, or reports a clear recovery path if a session looks interrupted (for example the active pointer exists but its session file is gone).

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

## recover

Repair a broken active-session pointer or clear stale command metadata after an
interruption. Saved sessions are preserved, including unreadable files that need
manual attention. A healthy active session is left alone.

```bash
debugbrief status
debugbrief recover
```

A held command lock is not stale. If cleanup is still running, wait briefly and
retry. Recovery does not kill an unrelated process or invent an outcome for a
command that stopped recording. Use `doctor` to inspect broader project health;
`doctor --fix` only creates local directories and the Git ignore entry.

## last

Prints the most recent report's path, its mode, modified time, and title. Add
`--open` to open it immediately.

```bash
debugbrief last
debugbrief last --open
```

## open

Opens a report in `$EDITOR` (latest by default, or `--path PATH`). If the
selected artifact is HTML, it opens in the system browser automatically. Use
`--html` to select the newest HTML artifact explicitly. If `$EDITOR` is not set
for a non-HTML report, it prints the path instead of guessing. Replace
`SESSION_ID` below with the id of the report you want to open.

```bash
debugbrief open
debugbrief open --html
debugbrief open --path ".debugbrief/reports/SESSION_ID-pr.md"
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
debugbrief end --mode pr --stdout | gh pr comment --body-file -
```

## Project configuration

An optional `.debugbrief.toml` at the project root sets defaults so you do not
retype the same flags. It is entirely optional: a missing file is ignored, and
lines that cannot be parsed are ignored as a whole rather than failing a command. Only
these keys are read:

```toml
default_mode      = "pr"        # default for end/preview (pr, handoff, incident)
timeout_seconds   = 600         # default for run/redo
detail            = "compact"   # optional report verbosity; CLI default is compact
```

`include_full_path` is intentionally CLI-only because absolute repository paths
are sensitive; a project config cannot enable it. Use `--include-full-path` to
opt in for one `preview` or `end`, or `--no-include-full-path` to make the
sensitive output explicitly false. An explicit flag always wins over the config,
and the config always wins over the built-in default. Parsed with the
standard-library `tomllib` on Python 3.11+
and the `tomli` backport on 3.9/3.10; a malformed file is ignored as a whole, and
`debugbrief doctor` flags it.

## Local storage

All state lives under the project root:

```text
.debugbrief/
  active_session.json            # pointer to the active session
  sessions/<session_id>.json     # the full, canonical session record
  reports/<session_id>-<mode>.md # generated reports (and .json/.html with --format)
```

The project root is the Git repo root when inside a repo, otherwise the current
working directory.

## Recovery for interrupted sessions

The canonical record is the session file under `.debugbrief/sessions/<id>.json`,
rewritten atomically after each saved note and command. Saved events survive a
crash, but a command still in progress may have no recorded outcome.
`active_session.json` is a small pointer removed after a clean `end`, an explicit
`cancel`, or a recovery that clears a broken/stale pointer. If the pointer exists
but the session file is missing, `status` reports it and prints recovery steps.

## Experimental shell mode

Capturing arbitrary shell history reliably across shells and platforms is hard
and easy to get subtly wrong. Rather than ship something that silently produces
incomplete or fabricated history, `start --shell` is not implemented. Running it
prints a clear message pointing to the reliable model: explicit `debugbrief run`.

This is different from `run --shell`, which runs a single command through the
shell and is supported. Prefer Bash, which lets DebugBrief enable `pipefail`;
a fallback shell may not provide a trustworthy pipeline status.

## Snapshot-tested reports

Report rendering is covered by snapshot tests under `tests/snapshots/`. The
committed snapshots double as concrete sample reports. To regenerate them on
purpose:

```bash
DEBUGBRIEF_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py
```
