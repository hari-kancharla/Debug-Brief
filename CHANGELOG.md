# Changelog

All notable changes to DebugBrief are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.0] - 2026-06-17

### Added

- `debugbrief init` sets up a project in one step: it creates and locally
  ignores `.debugbrief/`, reports health, and prints the recommended `db` alias
  and the workflow. It never starts a session or writes a report.
- `debugbrief recover` repairs a broken or stale active-session pointer left by a
  crash or an interrupted finalize so a new session can start. A healthy active
  session is left untouched, and corrupt session files are reported, not deleted.
- `end --detail compact` (and `preview --detail compact`) writes a shorter PR
  brief: the summary, changed files, and verification stay visible while the
  metadata and timeline fold into a collapsible section. The default stays full.
- An optional `.debugbrief.toml` at the project root sets defaults for the report
  mode, the run timeout, and the report detail. An explicit flag always wins. It
  is parsed with the standard-library `tomllib` on Python 3.11+ and the `tomli`
  backport on 3.9/3.10 (DebugBrief's one conditional dependency, `tomli>=2.0.1`,
  installed only below 3.11). A malformed file is ignored as a whole rather than
  partially applied, and `debugbrief doctor` flags it. Only top-level keys are
  read, so a key nested under a `[section]` never alters a setting.

### Fixed

- A captured command now holds an exclusive lease for its whole lifetime, so
  there is no window where a command is running yet the session can be finalized.
  A second `run`/`redo` is refused while one is active, and `end`/`cancel` refuse
  until it finishes. The lease is an OS-held lock (released automatically on
  crash) plus readable `active_command.json` metadata; `recover` clears a stale
  lease, preserving the session and noting the lost command. Each command has a
  unique id, so a retried persistence cannot record it twice.
- `redo` now re-runs the command in the directory it was originally captured in,
  not wherever `redo` was invoked, which matters in monorepos. It fails without
  running if that directory is gone, and falls back to the current directory with
  a warning for older sessions that did not record it.
- Dirty-file fingerprinting inspects a path with `lstat` before opening it, so
  starting or ending a session never blocks on a FIFO or follows a symlink: a
  special file gets a type sentinel, a symlink hashes its target string, and an
  unreadable file falls back to stat metadata.
- DebugBrief refuses to follow a symlinked `.debugbrief/` (or its `sessions/` /
  `reports/`) or a symlinked lock file, failing with a clear message and using
  `O_NOFOLLOW` on the lock where supported, so state cannot be redirected outside
  the project.
- A timeout now terminates the command's process group, not just the immediate
  process. The command runs in its own process group (`start_new_session`) and a
  timeout signals the group (SIGTERM then SIGKILL), so a command that spawned
  background children no longer leaves them running. A child that detaches into
  its own session (`setsid`) but keeps a captured stream open is reported as a
  warning; one that also closes its inherited output descriptors can outlive the
  command undetectably.
- `run` no longer hangs when the command exits but a background process it
  started keeps the output stream open (for example `sh -c 'server &'`). The
  runner notices the reader threads are still active, drains what is buffered,
  and returns with a warning instead of blocking indefinitely.
- The retained preview is bounded in memory while the command runs. Output flows
  through a head-and-tail buffer capped at the preview budget, so a command that
  prints gigabytes no longer grows the runner's memory; previously the full
  output was held before truncation.
- Ctrl-C during a command now terminates the process group and records the
  command with an `interrupted` status, instead of killing the run without
  recording the attempt. The stored event keeps the raw code the child died with
  while the CLI exits 130. The interrupt is handled across the whole wait and
  drain lifecycle, so it cannot escape unrecorded.
- A command killed by a signal propagates the conventional `128 + N` exit code
  (so SIGINT is 130, SIGSEGV is 139), instead of the raw negative code that a
  shell would turn into the wrong status. The raw signal code is still stored.
- A failed second pseudo-terminal allocation no longer leaks the first pair of
  descriptors before falling back to pipes.
- Repeated Ctrl-C is handled deliberately instead of crashing the runner. A
  single interrupt terminates the command's process group and records the
  command as interrupted, and a second interrupt arriving while the first is
  being handled is absorbed so teardown can finish. Under an extreme burst of
  signals the process can still be killed before it records the event; in that
  case the session stays valid and the event is simply not written, never
  corrupted or half-written.
- Changed-file lists show non-ASCII and spaced filenames verbatim
  (`café_漢字.txt`, `file with spaces.txt`) instead of git's octal-escaped,
  C-quoted form, and renames report the new name. Git output is decoded as
  UTF-8 regardless of the process locale, so a C/POSIX locale no longer mangles
  or fails on such paths.
- `debugbrief run` piped into a consumer that closes early (`run -- yes | head`)
  now stops the command promptly instead of running it to the timeout. The
  closed downstream pipe is detected, the command's group is terminated, the
  event is recorded with a `broken_pipe` status, and the CLI exits 141 without a
  traceback.
- The observed-error section now searches a failed command's stdout when its
  stderr has no content. Many test tools (pytest among them) print assertion
  failures and the summary to stdout, so the section was previously empty for a
  failing pytest run. Failing commands are weighed in priority order, each one's
  stderr then its stdout, so the highest-priority failing command's error wins
  even when it is only on stdout and an unrelated lower-priority command failed
  with output on stderr.
- Generated Markdown stays valid for arbitrary commands, output, and filenames.
  Captured output containing a line of backticks can no longer close a code
  fence early, and a command or filename containing backticks no longer breaks a
  code span; delimiters are chosen longer than any run in the content.
- The stored preview drops unsafe terminal controls (bare carriage returns, BEL,
  backspace, and other C0/C1 controls), normalizes CR-LF and a bare CR to a
  newline even across read boundaries, and removes escape sequences (including
  long OSC and DCS) split across chunks, via a bounded state machine that never
  leaves a fragment. A string sequence (OSC/DCS/APC/PM/SOS) that runs past the
  internal length cap is discarded up to its terminator instead of leaking its
  tail into the report as text; a runaway control sequence (CSI/ESC), which is
  short by spec, is abandoned at the cap so normal output resumes.
- Captured commands run from the directory they are invoked in, not the
  repository root, so `cd packages/api && debugbrief run -- pytest` behaves the
  way it would if typed directly. State still lives at the repository root.
- Test and build recognition is anchored to the command's executable. A tool
  name that only appears as an argument (`echo pytest`) is no longer treated as
  a check, a path like `.venv/bin/pytest` is recognized by its basename, and
  common wrappers (`python -m`, `uv`/`poetry`/`pdm`/`hatch`/`rye run`, `bundle
  exec`, `npx`, `pnpm`/`yarn exec`) are unwrapped to the inner command. A known
  boolean flag (`npx --yes jest`, `uv run --no-sync pytest`) is skipped, and any
  other wrapper option consumes the following token as its value (`uv run --with
  pytest pytest`, `uv run --env-file .env pytest`), so an option's value is never
  read as the command and a non-test cannot pose as a passed test.
- Shell commands (`run --shell`) run through bash with `pipefail` set, so a
  pipeline exits nonzero if any stage fails, not only its last. A compound shell
  command (joined by `|`, `&&`, `||`, `;`, `&`, or a newline) is recorded as a
  single command and is never attributed to an internal tool, because an exit
  code cannot say which stage produced it: a failing `cd missing && pytest` is no
  longer recorded as a failed pytest, and a passing `pytest && ruff check .` is
  not recorded as only pytest. Run a check on its own, or declare the whole
  command with `--verify` (honored only when the exit code is reliable), to
  record a verification.
- The session title and warning messages are redacted before reaching disk, the
  same as command output. Auto-start redacts the full command before truncating
  it, so a secret cannot survive truncation into the title.
- `.debugbrief/` is created mode 0700 and reports mode 0600, regardless of the
  user's umask, so a project's debugging history is not left readable by other
  local accounts.
- Changed-file reporting compares against the working-tree state captured at
  session start, so a fix committed during the session is included and a file
  already dirty before the session and untouched since is no longer counted.
- Red-to-green pairs a failing check with a later pass only when it is the same
  command run in the same directory, not an unrelated check passing later.
- The report section "What was ruled out" is now "Failed attempts" (a failed
  command is an attempt that did not pass, not a proven ruled-out cause), and
  handoff/incident current state is judged by each check's latest outcome rather
  than any historical failure.
- Finalizing a session is transactional: reports are rendered, written
  atomically (temp file, fsync, rename), then the session is marked completed and
  the active pointer is cleared last, so a failure mid-finalize leaves the
  session recoverable instead of completed without a report.
- Concurrent command recording is serialized with a per-repository advisory lock
  so two terminals finishing at once cannot lose each other's event.
- JSON-only reports (`end --format json`) are found by `last`, `list`, and
  `show`, which previously looked only for Markdown.
- The pseudo-terminal inherits the user's terminal size instead of a fixed
  24x80, so captured output wraps the way the user sees it.

### Changed

- `run` now executes the command under a pseudo-terminal (one each for stdout
  and stderr) instead of plain pipes, so output streams live with terminal-like
  buffering. Most programs block-buffer when they detect they are not on a
  terminal, which previously meant a plain script's output only appeared once it
  exited; under the pty it streams line by line. stdout and stderr stay separate,
  terminal escape sequences are stripped from the stored preview as output is
  read (so a sequence split across reads or the truncation boundary leaves no
  fragment; the live echo keeps color), the preview is bounded, and capture
  falls back to plain pipes where no pty can be allocated. Standard library
  only; still no runtime dependencies.

## [1.2.0] - 2026-06-11

### Added

- Recognition for today's test runners: vitest, bun test, deno test,
  node --test, make test and make check, tox, unittest, dotnet test, ctest,
  phpunit, mix test, and swift test join the existing table. Wrapped
  invocations such as `npx vitest` or `python -m unittest` match the same way.
- `run --verify` (also on `redo`) declares an unrecognized command, such as a
  custom test script, as a check. It counts as verification only when it
  really exits 0; a failing declared check stays a failure and feeds the
  reproduce line and the red-to-green window. On a recognized runner the flag
  is a no-op, and a redo of a declared check inherits it automatically.
- `debugbrief preview [--mode pr|handoff|incident]` prints the report for the
  active session to stdout without ending the session, writing a file, or
  modifying anything; the output carries a one-line preview banner.
- Sample handoff and incident reports under `examples/`, generated with the
  tool itself from realistic sessions.

### Changed

- The README documents the recognized runner table, points custom scripts at
  `--verify`, and summarizes what redaction does and does not catch.

## [1.1.0] - 2026-06-11

### Added

- Derived reports. Reports now reconstruct the investigation from recorded
  events instead of restating counts: a one-line summary built from true facts,
  reproduce/verify commands, a red-to-green window that correlates the files that
  changed between a failing and a passing check (correlation, never claimed
  cause), a full timeline with per-command durations, the observed error quoted
  verbatim, and what was ruled out. Sections render only when they have real
  content.
- Per-command git snapshots. Each captured command records the HEAD short SHA and
  the changed-file set at that moment, used to correlate changes in the
  red-to-green window. Best effort and backward compatible.
- Secret redaction at capture time. Captured stdout/stderr previews and the
  command text are scrubbed of common secret shapes (provider keys, bearer and
  authorization values, private key blocks, `scheme://user:password@host`
  connection strings, and sensitive `name=value` pairs) and replaced with
  `[redacted]` before anything is written. On by default; `debugbrief run
  --no-redact` stores raw text. Reports note when redaction was applied.
- JSON report output. `debugbrief end --format md|json|both` (default `md`)
  writes a structured JSON report with the same derived fields next to the
  markdown.
- Auto-start. `debugbrief run` and `debugbrief note` auto-start a session (with
  clear notice) when none is active, so a capture is never dropped.
- A documented one-line recipe for posting a brief to a pull request with the
  GitHub CLI.
- `debugbrief run -- <command ...>` passthrough form. DebugBrief flags
  (`--shell`, `--timeout`, `--no-redact`) go first, then `--`, then the command
  exactly as you would normally type it, with no quoting needed; tokens after
  the `--` (including ones that look like flags, such as `-q`) are never parsed
  as DebugBrief options. Multi-token commands are reconstructed with
  `shlex.join`, so arguments containing spaces or quotes survive intact into
  storage and reports. The single quoted-argument form keeps working unchanged.
- `debugbrief redo` re-runs the most recently captured command in the active
  session with the same shell mode it was originally run with, records the
  result as a new event, and returns the command's own exit code, completing
  the run/edit/redo loop. It accepts `--timeout` and `--no-redact`, reports
  clearly when there is no session or no captured command yet, and refuses to
  re-run a stored command that contains the `[redacted]` placeholder, since
  that is not the real command.
- `debugbrief end` no longer requires `--mode`; it defaults to `pr`. A new
  `--stdout` flag prints the rendered markdown report to stdout while still
  writing the files, with every informational line moved to stderr, so posting
  a brief is one pipe: `debugbrief end --stdout | gh pr comment --body-file -`.
- `debugbrief note` accepts unquoted prose: multiple tokens are joined with
  single spaces, so `debugbrief note remember to check the lock ordering` just
  works. The quoted single-argument form is unchanged, and notes still go
  through redaction before they are stored.
- `debugbrief cancel` discards the active session without writing a report.
  The session file is kept with the new `ABANDONED` status (it still appears in
  `list` and `show`), the active pointer is cleared, and nothing is reported.
  It confirms before discarding; `--yes` skips the prompt, and anything other
  than `y` aborts safely with the session left untouched.
- README documents a one-line alias (`alias db="debugbrief run --"`) for daily
  use, and the limitations now note that interactive and TUI commands behave
  oddly under `run` because output is piped for capture.

### Fixed

- Piping command output to a consumer that closes early (for example
  `debugbrief show <id> --json | head -1`) no longer crashes with a
  BrokenPipeError traceback. The CLI exits quietly with code 141
  (128 + SIGPIPE), the Unix convention.
- Redaction now runs in linear time on long unbroken text. The previous
  key-scanning pattern was quadratic, so a pasted log line or captured
  base64-like output could stall capture for minutes; 200k characters now
  redact in milliseconds with identical results.
- Auto-started session titles are seeded from the command tokens as typed
  instead of the shlex-quoted reconstruction, so `list` shows
  `Auto session ...: python -c print(...)` rather than nested quote noise.
  The stored and executed command is unchanged.
- `debugbrief run` now streams the command's stdout and stderr live to the
  terminal, line by line and unmodified, while the command runs. Previously the
  output was captured but never shown, so a failing test run displayed no
  traceback at all. The full output is still accumulated for the bounded,
  redacted previews, a timeout keeps whatever partial output had arrived, and
  DebugBrief's own status lines moved to stderr so the wrapped command's stdout
  stays clean for piping.

- Redaction no longer masks values whose key merely contains a sensitive
  substring. The sensitive token must now be a whole, separator-delimited
  segment of the key, so `monkey:`, `turkey_count=`, `rapid_mode=`, `apiary`,
  and `keyboard=` are left intact while `password=`, `api_key`, `api-key`,
  `apikey`, `aws_secret_access_key`, `*_token`, and a bare `key=` are still
  redacted.
- Free-text notes are now scrubbed through the same redaction pass before they
  are written to the session file, so a secret pasted into a note (an env var or
  a log line) no longer lands raw on disk or in a report.
- Output truncation keeps both the head and a larger tail of long output (head
  is the first third of the budget) with an elision marker in between, instead
  of only the head. End-of-output content such as a trailing error or traceback
  now survives truncation.
- The changed-file summary excludes generated and cache artifacts
  (`__pycache__`, `*.pyc`/`*.pyo`, `*.egg-info`, `node_modules`, `.DS_Store`,
  and common cache directories such as `.pytest_cache`, `.mypy_cache`, and
  `.ruff_cache`) so reports list meaningful changes only.
- Resolved static type-checking errors in the redaction rule table, the
  changed-file accumulator, and the report-open target path.

### Changed

- The PR report dropped the templated Overview paragraph and the
  "No outstanding risks were detected" line in favor of the derived sections.
- Handoff next steps are now drawn only from recorded notes, never inferred.
- The "Modified files" section is omitted when there are no changes, rather than
  printed with a placeholder.
- README rewritten to be short and scannable, with the full per-command
  reference moved to `docs/COMMANDS.md` and a real sample report in
  `examples/sample-pr.md`.
- Packaging metadata uses the PEP 639 SPDX `license = "MIT"` string and
  `license-files` instead of the deprecated license table and classifier, so the
  build is warning-free (requires setuptools 77+ to build).
- Packaging declares `[project.urls]` (homepage, repository, changelog, issues)
  and classifiers for the full tested Python range, 3.9 through 3.13.
- CI now also runs Python 3.9 (the supported floor) and 3.13, so the advertised
  range is continuously verified on Linux and macOS.

### Design notes

- The per-command snapshot uses two lightweight git calls (`rev-parse --short`
  and `status --porcelain`) rather than literally one, so it can record both the
  HEAD and the changed-file set. It runs only inside a repo and is best effort.
- Redaction can modify the stored command string. This supersedes the earlier
  "command is always preserved verbatim" wording; use `--no-redact` for verbatim
  storage.
- Red-to-green requires a git repo. When a transition exists but no tracked files
  changed in the window, the section shows the window and states plainly that no
  file changes were recorded.
- The observed-error section appears in incident mode (the mode that calls for a
  verbatim error); pr and handoff surface failures under "What was ruled out".
- Handoff next-step detection matches forward-looking words on word boundaries
  so "retry" is not mistaken for "try".
- The README references a real sample report (`examples/sample-pr.md`, generated
  from an actual run with only the absolute path sanitized) and an inline
  quickstart in place of a demo gif, since no gif asset is shipped.
- `tests/test_reporters.py` assertions were updated to the new report structure;
  removing the Overview/Risks sections is incompatible with the previous
  assertions.

## [1.0.0] - 2026-06-07

### Added

- Local-first, dependency-free CLI with the `debugbrief` entrypoint, plus a
  `python -m debugbrief` entrypoint.
- Session lifecycle commands: `start`, `note`, `run`, `end`, `status`.
- Real command execution through `debugbrief run` with honest exit-code capture,
  bounded stdout/stderr previews with truncation flags, timeouts, and an
  explicit `--shell` mode.
- Deterministic command classification (test / build / lint / typecheck),
  report noise filtering, and deduplication.
- Native `git` state capture: branch, detached HEAD, initial/final SHA,
  name-status changed files (M/A/D/R), and shortstat.
- Markdown report generation for `pr`, `handoff`, and `incident` modes, built
  only from recorded evidence. No AI and no invented summaries.
- `debugbrief doctor [--fix]` health check with PASS/WARN/FAIL output.
- `debugbrief last` and `debugbrief open` for locating and opening reports.
- `debugbrief list` to list recorded sessions in reverse chronological order,
  with an optional `--json` summary.
- `debugbrief show <session_id>` for a compact session summary, with short id
  prefix resolution and an optional `--json` output of the full session.
- Local storage under `.debugbrief/` with `.git/info/exclude` integration that
  never modifies a shared `.gitignore`.
- Snapshot-tested reports, an end-to-end test that drives the real CLI through
  subprocess against a temporary Git repository, and a full test suite.
- GitHub Actions CI running tests on Python 3.10, 3.11, and 3.12, building the
  wheel, installing it, and running CLI smoke checks.
- Release files: `LICENSE` (MIT), `CHANGELOG.md`, `CONTRIBUTING.md`, and
  `.gitignore`.
