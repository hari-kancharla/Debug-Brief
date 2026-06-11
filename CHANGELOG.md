# Changelog

All notable changes to DebugBrief are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Notes on decisions not fully specified

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
