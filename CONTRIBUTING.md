# Contributing to DebugBrief

Thanks for your interest in improving DebugBrief. This is a small, focused,
local-first tool. Contributions that keep it honest, dependency-free, and easy
to reason about are very welcome.

## Project principles

DebugBrief intentionally does NOT:

- use AI or any model,
- make network calls at runtime,
- capture shell history,
- run a background daemon,
- ship a web UI or TUI,
- add runtime dependencies (standard library plus native `git` only).

Please keep changes additive and avoid breaking existing behavior or removing
edge-case handling.

## Setup

Requires Python 3.9+ on a Unix-like system (Linux, macOS, BSD) and native
`git`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run the tests

```bash
python -m pytest
```

To regenerate report snapshots intentionally (only when a report change is
deliberate):

```bash
DEBUGBRIEF_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py
```

## Build the wheel

```bash
python -m build
```

## Run the CLI locally

```bash
pip install -e .
debugbrief --help
debugbrief doctor
debugbrief start "trying something out"
debugbrief run "python -c 'print(1)'"
debugbrief end --mode pr
debugbrief list
```

You can also run it without installing the console script:

```bash
python -m debugbrief --help
```

## Contribution expectations

- Keep the runtime dependency-free. If a runtime dependency is truly
  unavoidable, justify it clearly in the pull request and README.
- Add or update tests for any behavior you change. Core logic must stay tested.
- Keep CLI output plain and human-readable. 
- Be honest in code and docs. Do not fabricate report content, root causes,
  test results, or verification, and do not claim a command was run unless it
  was actually run. Do not include AI-generated fake summaries or fabricated
  test claims.
- Keep comments meaningful. Explain intent or constraints, not the obvious.

## Pull requests

- Describe what changed and why.
- Confirm `python -m pytest` passes and the wheel still builds.
- Keep changes focused; prefer several small PRs over one large one.
