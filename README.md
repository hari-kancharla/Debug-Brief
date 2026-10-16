# DebugBrief

[![CI](https://github.com/hari-kancharla/Debug-Brief/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/hari-kancharla/Debug-Brief/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/debugbrief/1.4.1)](https://pypi.org/project/debugbrief/1.4.1/)

DebugBrief saves the commands you run while debugging, their results and the
notes you add. When you're done, it puts them into a report for a pull request,
a handoff or an incident review.

The report includes exit codes, output previews and Git changes. DebugBrief
doesn't use AI or guess a root cause.

![A failing test, a fix, a passing rerun and the generated report](https://raw.githubusercontent.com/hari-kancharla/Debug-Brief/main/docs/demo.gif)

## Install

Python 3.9 or newer on Linux or macOS. The project you debug can use any language.

```bash
pipx install debugbrief
# or
uv tool install debugbrief
```

Upgrade an existing installation with `pipx upgrade debugbrief` or
`uv tool upgrade debugbrief`. Inside an activated Python environment, use
`python -m pip install --upgrade debugbrief`.

The HTML and Explorer workflows below require DebugBrief 1.4.0 or newer.
Check your installed version with `debugbrief --version`. Install your project's
test tools and dependencies separately; DebugBrief does not install them for you.

## Quickstart

Run this from your project, replacing the pytest command with your usual check:

```bash
debugbrief run -- python -m pytest -q
# If it fails, edit the files and run the same command again:
debugbrief redo
debugbrief end --format html --open
```

The first `run` starts a session automatically. A title and notes are optional:

```bash
debugbrief start "Handle an expired cache entry"
debugbrief note "Expired entries still reach the read path."
```

Use `start` before your first captured command if you want to set the title.
Put DebugBrief flags before `--`; everything after it is the command to capture.
An optional alias saves some typing:

```bash
alias db="debugbrief run --"
db pytest -q
```

## Reports

| Mode | What it contains |
| --- | --- |
| `pr` | Reproduce and verify commands, changed files, failed attempts and a red-to-green window when available. |
| `handoff` | Current status, notes, command history and next steps drawn from your notes. |
| `incident` | A timeline, the recorded error, current outcome and follow-up notes. |

`end` defaults to a compact PR report in Markdown. Use `--detail full` for the
complete Markdown record. JSON and HTML always retain the complete evidence.

```bash
debugbrief end --format all    # Markdown, JSON and HTML
debugbrief open --html         # open the newest HTML report
```

The other formats are `md`, `json`, `both` (Markdown and JSON), and `html`.
Use `preview --format html` or `preview --format json` to inspect an active
session without ending it.

Example reports:
[PR](https://github.com/hari-kancharla/Debug-Brief/blob/main/site/data/sample-pr.md),
[handoff](https://github.com/hari-kancharla/Debug-Brief/blob/main/site/data/sample-handoff.md),
[incident](https://github.com/hari-kancharla/Debug-Brief/blob/main/site/data/sample-incident.md).
These are generated from sample data. The incident example includes a later
failure and an incomplete recording.

Review a report before sharing it. To post a reviewed Markdown brief with the
optional GitHub CLI:

```bash
debugbrief end --stdout | gh pr comment --body-file -
```

## What the evidence means

- A report counts as verified when at least one check passes, every recorded
  check has a valid time, and no check is still failing. A previous failure isn't
  required. Help, version and collection/list commands don't count, even with
  `--verify`. Use that flag to mark a custom command as a check.
- A red-to-green window requires Git and the same command failing and then passing
  in the same directory. Files changed during that window are a correlation,
  not a proven cause. A later failure keeps the report unverified.
- `redo` runs the stored command in its recorded directory using the current
  files, environment and dependencies. It does not recreate an old environment.
  `redo --dry-run` shows what would run; potentially destructive reruns ask for
  confirmation. `--yes` skips that prompt.
- Reports retain project-relative command directories. Missing directory data in
  older reports stays unknown. The project-root field uses the repository name
  by default; absolute paths inside commands, notes and output can still appear.
- Stored text goes through best-effort secret redaction. Live command output is
  shown as the command prints it. Redaction can miss secrets, and `--no-redact`
  disables it for a captured command. Review reports before sharing.

Verification covers the recorded checks, not the whole issue. Full capture and
privacy details are in [SECURITY.md](https://github.com/hari-kancharla/Debug-Brief/blob/main/SECURITY.md).

## Commands

| Command | Purpose |
| --- | --- |
| `init` | Set up local state and show the workflow. |
| `start "<title>"` | Start a named session. |
| `note <text>` | Record an observation. |
| `run -- <command>` | Run and capture a command. |
| `redo` | Run the last captured command again. |
| `preview` | Print a report without ending the session. |
| `end` | Finish the session and write reports. |
| `cancel` | Abandon the session, keeping its local record. |
| `status` | Inspect the active session and command. |
| `doctor` | Check the project and local state. |
| `recover` | Repair stale state after an interruption. |
| `last` / `open` | Find or open a saved report. |
| `list` / `show <id>` | Inspect earlier sessions. |

See [the command reference](https://github.com/hari-kancharla/Debug-Brief/blob/main/docs/COMMANDS.md)
for flags, configuration, recognized runners and recovery behavior.

## Dependencies and limits

The CLI uses the Python standard library and native Git. Python 3.9 and 3.10 also
need `tomli`, installed automatically to read optional TOML configuration.
DebugBrief itself sends no telemetry and makes no network requests. Commands you
run can still use the network.

Linux and macOS are tested in CI on Python 3.9–3.14. Native Windows and PowerShell
are not supported. Capture is explicit through `run`; it does not read shell
history or meaningfully capture full-screen programs such as vim or htop. Output
previews are bounded, so reports are not complete terminal transcripts.

Ordinary background descendants are stopped during command cleanup. Deliberately
detached processes can survive; run long-lived servers outside DebugBrief.
Outside a Git repository, command and note capture still work, but Git state,
changed-file correlation and red-to-green windows are unavailable.

## Website and development

The static site includes a command reference and a JSON Evidence Explorer.
Imported reports stay in memory in the browser; they are never uploaded or stored.
An explicitly chosen color theme is saved. Fonts and optional homepage animations
load from a CDN. Generated HTML reports are self-contained and need no CDN.

From a source checkout, preview the site with `python3 scripts/serve_site.py`.
The site is included in the source archive, not the installed wheel.

For Python development:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

[CONTRIBUTING.md](https://github.com/hari-kancharla/Debug-Brief/blob/main/CONTRIBUTING.md)
covers setup, linting, types, browser checks and builds.

## License

MIT. See [LICENSE](https://github.com/hari-kancharla/Debug-Brief/blob/main/LICENSE).
