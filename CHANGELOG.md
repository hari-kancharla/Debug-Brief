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

### Fixed

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
