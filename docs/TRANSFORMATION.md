# DebugBrief product transformation

Historical record from before the supplied ZIP site replaced the earlier layout.
Its browser results do not verify the current site; the maintained browser suite
in `tests/browser/site.spec.js` covers the current design.

## Constraints

Local changes only. No push, publishing, credential changes, or README.md edits.
Preserve public commands, flags, session compatibility, redaction, locking, and rerun safeguards.
Use the existing standard-library Python and buildless HTML/CSS/JavaScript stack.

## Baseline

2026-10-15, Python 3.9.6 on macOS: 573 tests passed; Ruff and mypy passed.
Initial isolated package build could not fetch setuptools because sandbox networking was restricted.
Baseline screenshots are in ignored `artifacts/verification/`.

## Implementation sequence

1. Add compatible structured command history to JSON reports; retain execution errors in captured evidence.
2. Refine HTML report readability, output inspection, status cues, mobile and print behavior.
3. Unify the site around an engineering field notebook: warm paper, dark ink, restrained green/red, compact full-width sections.
4. Build a working Evidence Explorer with strict, bounded, in-memory JSON import and safe text rendering.
5. Generate reproducible example reports with the actual Python renderers. Mark fixture evidence as illustrative.
6. Unify documentation, verify supported commands, fix stale links and missing anchors.
7. Add internal-link, fixture-parity, import-security, interaction, responsive and accessibility regression checks.
8. Prepare GitHub Pages deployment with main-branch gating; run final Python, package and browser checks.

## Research informing the implementation

- [Playwright Trace Viewer](https://playwright.dev/docs/trace-viewer): local browser file inspection and chronology that opens into detail. Applied to the Explorer; imported content must not trigger network requests.
- [uv tools](https://docs.astral.sh/uv/guides/tools/): isolated CLI installation and compact, actionable command documentation. Applied to onboarding.
- [WAI-ARIA tabs](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/): roving focus, arrow/Home/End keys, selected state and labelled panels.
- [GitHub Pages custom workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages): artifact-based deployment with scoped permissions and a deployment environment.

The research informs interactions and information hierarchy, not a copied visual design.

## Implemented outcome

The site now shares one design and interaction layer across the homepage, documentation, and Evidence Explorer. It uses plain text controls, compact layouts, real generated report previews, and no decorative arrows, status dots, fake record numbers, external fonts, telemetry, or framework runtime. Obsolete alternate site assets were removed. The user's visual correction was applied across all three pages. The bracketed [db] wordmark is retained in each header and footer, with tighter spacing and a consistent monospace treatment.

The Python product changes are additive:

- Structured JSON schema version 1 retains every command attempt, actual status, exit code, duration, bounded output, truncation flags, and current failures. Existing report fields and session storage remain compatible.
- Spawn and command-parse failures retain their real diagnostics in the bounded, sanitized, redacted command record. Missing executables receive a PATH/environment hint.
- Red-to-green correlation compares known content fingerprints against the failed check. A file unchanged across the check window is excluded even when it changed earlier in the session. Older snapshots retain their existing fallback behavior.
- HTML reports expose safely escaped command output, distinguish failed and passing timeline entries, and reveal command details during printing in the tested browser.
- The Explorer supports current and older JSON reports, file picking and dropping, per-attempt output, search and status filters, notes, changes, chronology, verification, evidence scope, missing fields, and warnings. Imports remain in memory, with no upload, browser storage, or execution. Limits are 5 MiB, 2,000 items per list, bounded nesting, node count, and field lengths. Contradictory outcomes are rejected, and failed imports preserve the current report.
- An independent process supervisor stops the owned command group after recorder death, including SIGKILL, and reaps normal background work. Recovery preserves missing outcomes as unknown. See the [final platform and crash-cleanup evidence](PLATFORM_VERIFICATION.md) for tested coverage and detached-process limits.

The examples are **illustrative fixture inputs rendered by the actual Python reporters**, not claims about a real debugging session. A separate installed-wheel smoke report was generated from actual executed commands and successfully imported into the Explorer.

## Final local verification

| Check | Actual result |
| --- | --- |
| Existing baseline | 573 tests passed before changes |
| Full Python suite | 618 per interpreter on macOS ARM64 and Linux ARM64, Python 3.9–3.14; final platform evidence records additional architecture coverage |
| Ruff | Passed for src, tests, and scripts |
| mypy | Passed for all 25 source files |
| Package build | Isolated source distribution and wheel build passed |
| Clean wheel installation | Passed; pip check reported no broken requirements |
| Installed-wheel workflow | Real exit codes 7 and 0, captured output, all report formats, and honest unverified state passed |
| Browser suite | 72 Chromium, 66 Firefox, and 66 WebKit checks passed; clipboard permission and native WebKit differences are documented in the platform evidence |
| Viewports | 375, 768, and 1440 CSS pixels; additional 1920-pixel report layout and 320-pixel reflow and 200 percent CSS scaling |
| Accessibility | Axe WCAG A/AA checks through 2.2 on all six pages at three viewports; keyboard, focus, reduced motion, menu, tabs, labels and announcements exercised |
| Import security | Malformed JSON, incompatible schema, contradictory results, missing fields, oversized input, injection text, zero import requests, and empty browser storage passed |
| Hosting paths | All local links/assets/fragments/ARIA references resolve; browser navigation and sample loading work under /Debug-Brief/ |
| Fixtures | Nine generated outputs match the Python renderers byte for byte |
| README.md | Unchanged |
| Git | Primary repository HEAD unchanged; no push, tag, package publication, or deployment performed |

Logs, JSON audit output, screenshots, print output, and actual wheel smoke artifacts are under ignored `artifacts/verification/`. Initial packaging network restrictions were resolved with an isolated approved build; they are not an outstanding failure.

### Reproduce locally

Use an activated Python environment with `pip install -e '.[dev]'`, then:

```sh
python -m pytest -q
python -m ruff check src tests scripts
python -m mypy src/debugbrief
python -m build
python scripts/check_site.py
python scripts/generate_samples.py --check
npm ci
npx playwright install chromium
npm run test:site
```

For a local preview: `python scripts/serve_site.py`. It binds only to `127.0.0.1:8765`. Browser tests start this server automatically if one is not already running. To use an existing isolated Chrome executable, set `DEBUGBRIEF_BROWSER_PATH`. With the preview running, `node scripts/audit_site.mjs` reproduces the Lighthouse measurements. To check a clean wheel environment, run `python scripts/verify_wheel.py path/to/environment/bin/debugbrief`.

## Security review and limits

Imported text never becomes HTML, CSS, a URL, or an executed command. Rendering uses DOM text nodes. Sample fetching uses a fixed allowlist. The Explorer uses a restrictive Content Security Policy. Imported reports are not independently authenticated or rerun; the UI explicitly identifies their statuses as reported claims. A report can still contain secrets or absolute paths in command text, output, or notes; redaction remains best effort. New shareable command records omit invocation directories and fingerprint maps.

Command execution, replay confirmation, process handling, atomic state writes, filesystem guards, session locks, and recovery protections remain in place and are covered by the retained suite. There is no new Python runtime dependency and no mandatory network or AI service.

Local testing does not establish the Linux/macOS and Python 3.9–3.14 matrix; the existing CI matrix remains configured but was not run remotely. Safari, Firefox, physical mobile devices, manual screen-reader testing, and a formal accessibility certification were not completed. CSS scaling and viewport reflow were exercised; browser-native zoom across all engines remains unverified. Print disclosure behavior was checked in Chrome for Testing. Old reports without fingerprints cannot support the newer content comparison and retain the documented fallback. Reports with more than 2,000 entries per list must be inspected as HTML/Markdown or reduced before browser import.

## Deployment preparation

GitHub Pages is prepared, not deployed. The manual workflow accepts only main in the current repository, runs the site validation workflow first, uploads only `site/`, and scopes deployment permissions to its deploy job. Pull requests never deploy. No repository setting or credential was changed. After separately authorizing publication, the maintainer must select GitHub Actions as the Pages source, review environment protections, push the changes, and manually run the deployment workflow. No live URL is claimed. The relative social-card image path can be replaced by an absolute image URL once the final public origin is known.

Additional product research: [rr](https://rr-project.org/) makes deterministic process replay explicit; DebugBrief's `redo` instead performs a new execution in the current environment. The documentation now states that dependencies and inputs must be preserved separately for reproducibility. [GitHub job summaries](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#adding-a-job-summary) reinforce keeping a scannable Markdown summary alongside inspectable artifacts. These informed product boundaries and report hierarchy; no remote publishing behavior was added.

## Measured Lighthouse results

One local simulated run per form factor, after the design corrections:

| Mode | Performance | Accessibility | Best practices | SEO |
| --- | --- | --- | --- | --- |
| mobile | 99 | 100 | 100 | 100 |
| desktop | 99 | 100 | 100 | 100 |

These are local lab measurements, not deployed field data. Raw audits are saved under `artifacts/verification/`.

## Actual debugging demonstration

An installed wheel recorded a controlled reproduction of a confirmed DebugBrief source defect in an isolated local Git project under `artifacts/end-to-end-20261015/`. The original source incorrectly included unchanged content and changes made before the failed check in the failure-to-pass window. The same 37-test suite ran before and after a single source-function fix: first **2 failed, 35 passed** with exit 1; then **37 passed** with exit 0. Test files were unchanged. A separate `git diff --check` passed and was recorded as the third command.

The completed session generated HTML, Markdown, and JSON. The JSON was imported into the user-visible Explorer, and an isolated browser check verified original failure output, passing output, filters, the one changed source file, three notes, and the explicit evidence limits. The fixture has its own local baseline commit; the primary repository has no new commit. Nothing was pushed or uploaded. Original generated reports and their hashes remain preserved. The CLI transcript viewer is a clearly labeled demonstration aid displaying actual subprocess output, not a product terminal.

The original run exposed two presentation issues preserved in its historical artifacts: summary prose says "3 attempts" for two test executions plus one Git check, and the Explorer shows unrounded floating-point seconds for the window. Both were subsequently fixed in the product, with raw numerical evidence preserved. The historical artifacts were not rewritten. This session demonstrates the tested workflow; it does not establish broad incident coverage or market superiority.

After restoring the bracketed [db] wordmark, the then-current 63 browser checks passed again across the three configured viewports. The rerun exposed a test cleanup race in the repository-prefix check; it now waits for in-flight request handlers before disposing the browser context, using [Playwright's documented wait behavior](https://playwright.dev/docs/api/class-page#page-unroute-all). Production assertions were retained. Raw run results and current screenshots are saved with the demonstration artifacts.

## Precision and polish acceptance

The shared summary now explicitly labels the total as session commands instead of implying that every command was an attempt at the same check. It also names another currently failing verification even after the original check turns green. The Explorer and generated HTML/timeline durations show bounded milliseconds below one minute and compact minutes/hours above it. JSON retains raw duration values; display formatting does not alter imported evidence.

A fresh short session exposed a separate pre-existing aggregation defect: a failed run followed immediately by a passing run could be displayed as two passing verifications. The command aggregation now tracks actual valid passing verifications separately from its total attempt count. HTML and Markdown use the passing count; the full raw history and the existing total deduplication count remain intact. Regressions cover one failure plus one pass and one failure plus two passes.

The report timeline now separates timestamps from event text, stacks naturally on narrow screens, and avoids the leftover vertical ornament and standalone punctuation beneath follow-up commands. Site and artifact widths use the available desktop space with a cap of 1,800 pixels. Headers and footers retain [db]. Repository links include the unmodified [official Primer GitHub mark](https://primer.style/octicons/icon/mark-github-16/), with its MIT license saved alongside the asset; [GitHub's branding guidance](https://brand.github.com/foundations/logo) permits a secondary project link.

Final acceptance artifacts are under `artifacts/polish-20261015/accepted/`. The controlled source-defect reproduction uses 39 unchanged tests: **5 failed and 34 passed before the source correction; 39 passed after it**. The rebuilt wheel records real exits 1, 0, and 0, writes all three report formats, and ends with no active session. The exact JSON is imported into the Explorer. The manifest records test/report hashes and the built wheel hash. Browser acceptance checks verify the summary, precise displayed window, passing count, raw failures and passes, filters, changed file, notes, scope, zero report-import requests, and no browser errors.

The full final Python suite passes **603 tests**; Ruff, mypy, isolated sdist/wheel builds, installed-wheel dependency and workflow checks, link checks, and nine fixture parity checks pass. The browser suite passes **69 checks**. Local Lighthouse remains 99 performance and 100 accessibility, best practices, and SEO in both simulated modes. These are local macOS/Python 3.9 and Chromium results; the platform and manual accessibility limits above still apply. README.md and the primary repository HEAD remain unchanged. No push or publication occurred.



## Real-project validation

See [the real-project verification record](REAL_PROJECT_VERIFICATION.md) for the fresh CodeReview Arena run (1,144 passed, 8 skipped), 12 deliberate resilience scenarios, the working-directory export defect and fix, two-runtime DebugBrief suite results, artifact hashes, and bounded readiness verdict. The original Arena working files remain untouched.
