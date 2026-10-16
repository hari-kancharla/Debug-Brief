# Contributing to DebugBrief

Thanks for your interest in improving DebugBrief. This is a small, focused,
local-first tool. Contributions that keep it honest, lean, and easy
to reason about are very welcome.

## Project principles

DebugBrief intentionally does NOT:

- use AI or any model,
- make network calls at runtime,
- capture shell history,
- run a background daemon,
- run a web server as part of command capture. Self-contained HTML reports and
  the separate static website are supported,
- add new runtime dependencies. DebugBrief uses the standard library and native
  `git`, plus one conditional dependency: the `tomli` TOML parser on Python 3.9
  and 3.10, where the standard-library `tomllib` is not yet available.

Please keep changes additive and avoid breaking existing behavior or removing
edge-case handling.

## Setup

Requires Python 3.9+ on a Unix-like system and native `git`. Linux and macOS
are tested in CI; other Unix-like systems may work but are not currently tested.
Native Windows and PowerShell are not supported.

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

Run the same lint and type checks as CI:

```bash
python -m ruff check src tests
python -m mypy src/debugbrief
```

## Website changes

The site is plain HTML, CSS and JavaScript; there is no build step. From the
repository root, `python3 scripts/serve_site.py` starts an uncached preview at
`http://127.0.0.1:8765`. Stop it when you finish.

Check local links and the generated report fixtures:

```bash
python scripts/check_site.py
python scripts/generate_samples.py --check
```

For browser tests, install Node.js and the pinned tools in `package-lock.json`:

```bash
npm ci
npx playwright install --with-deps chromium
npm run test:site
```

The browser suite checks imports, keyboard interaction, accessibility, reflow and
report privacy. Firefox and WebKit checks are available through
`npm run test:site:firefox` and `npm run test:site:webkit` after installing those
Playwright engines. Engine tests do not replace physical-device or screen-reader
checks.

When report rendering changes, regenerate the nine illustrative site fixtures
with `python scripts/generate_samples.py`, review the output, then run the check
again. Do not edit generated reports by hand or pass them off as real executions.

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

- Keep the runtime lean: the standard library and native `git`, plus the
  existing conditional `tomli` backport on Python 3.9 and 3.10. Avoid adding new
  runtime dependencies. If one is truly unavoidable, justify it clearly in the
  pull request and README.
- Add or update tests for any behavior you change. Core logic must stay tested.
- Keep CLI output plain and readable.
- Be honest in code and docs. Do not fabricate report content, root causes,
  test results, or verification, and do not claim a command was run unless it
  was actually run. Do not include AI-generated fake summaries or fabricated
  test claims.
- Keep comments meaningful. Explain intent or constraints, not the obvious.

## Pull requests

- Describe what changed and why.
- Confirm `python -m pytest` passes and the wheel still builds.
- Keep changes focused; prefer several small PRs over one large one.

## Review and merge

`main` is protected: changes land through a pull request with CI passing and all
review conversations resolved, not by direct pushes. The repository owner
(@hari-kancharla, see [CODEOWNERS](.github/CODEOWNERS)) reviews and merges. Outside
contributions are welcome but are merged only after that review. Maintainer
protection settings are documented in [docs/maintainers.md](docs/maintainers.md).
