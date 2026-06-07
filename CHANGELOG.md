# Changelog

All notable changes to DebugBrief are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
