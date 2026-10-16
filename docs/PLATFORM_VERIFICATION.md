# Platform, crash cleanup, and final product verification

Historical record from before the supplied ZIP site replaced the earlier layout.
Its browser results do not verify the current site; the maintained browser suite
in `tests/browser/site.spec.js` covers the current design.

Date: 2026-10-15. Local changes only; no push, publication, or deployment.

## Readiness boundary

The tested CLI, report generation, recovery, and Explorer flows are suitable for
a local beta. The checks below establish specific behavior, not a guarantee that
every project or host environment will behave identically. Review reports before
sharing them: secret redaction remains best effort.

## Interpreter and operating-system coverage

The complete **618-test** Python suite passed for each version below on native
macOS ARM64, Linux ARM64, and emulated Linux x86-64. Every row also passed mypy, source/wheel builds,
a clean wheel installation, dependency consistency, and the installed CLI flow
that records real failure and success, generates all formats, and checks scope.

| Python | macOS ARM64 | Linux ARM64 | Linux x86-64 |
| --- | --- | --- | --- |
| 3.9.25 | Passed | Passed | Passed (emulated) |
| 3.10.20 | Passed | Passed | Passed (emulated) |
| 3.11.15 | Passed | Passed | Passed (emulated) |
| 3.12.13 | Passed | Passed | Passed (emulated) |
| 3.13.14 | Passed | Passed | Passed (emulated) |
| 3.14.6 | Passed | Passed | Passed (emulated) |

macOS is 27.0.1 on Apple Silicon. Linux uses Debian Bookworm containers on Docker
Desktop's Linux kernel, with networking disabled during verification and copied
source files. The x86-64 runs use QEMU emulation, not physical x86-64 hardware.
The emulated x86-64 image uses Python's pip/venv tools because the x86-64 `uv`
binary crashed in the emulator before executing product code. Ruff is checked
on the native macOS and Linux ARM64 rows rather than through that emulator.

Initial harness problems are preserved in the evidence: scratch directories
inside the enclosing Git checkout, an offline dependency wheel with the wrong
interpreter tag, and an attempted reuse of an existing clean wheel environment.
These were corrected before the final results. One x86-64 retry was interrupted when its hosting matrix container exited; it was repeated to completion in a dedicated container. Emulation also exposed a race in
the crash test's PID readiness file; it now publishes that file atomically.

## Crash cleanup implemented and exercised

The command now runs beneath an independent standard-library Python supervisor.
Only the recorder owns the write end of its lifetime pipe. On recorder death,
including `SIGKILL`, EOF makes the supervisor terminate its own command group,
then escalate to `SIGKILL` after a half-second grace period. The supervisor stays
the group's leader throughout cleanup, anchoring its identity. Commands do not
inherit the lifetime pipe. Normal completion drains output, stops ordinary
background descendants, and reaps the supervisor.

Actual subprocess tests cover PTYs and forced pipe fallback, `SIGTERM`-resistant
commands and descendants, descendants with closed output and lock descriptors,
startup EOF, repeated interrupts, lock release, recovery followed by a new
capture, unrelated-process survival, normal completion, and descriptor-exhaustion
cleanup. Assertions run before any emergency test cleanup. A real-project stress
session also killed the recorder and verified automatic cleanup without sending
any child signals from the harness.

Recovery preserves the session and explicitly warns that the lost command did
not finish recording. It does not invent a result or passing verification.
Recovery can report the command as running during the short cleanup interval;
it never clears a held lock. A child that deliberately creates a separate
session/process group is outside this cleanup guarantee. If it also closes its
captured output, the recorder cannot detect its continued existence.

The implementation follows Python's documented [subprocess descriptor and
session controls](https://docs.python.org/3.14/library/subprocess.html#popen-constructor)
and [POSIX process-group signals](https://docs.python.org/3.14/library/os.html#os.killpg).
It uses no unsafe pre-exec hook, process-name scanning, service, or new dependency.

## Fresh real-project run

Copied the current CodeReview Arena working source as-is into an isolated local
project: **681 source files**, including its existing uncommitted work. No defect
was inserted. Ran its actual lint, formatting, type checks, and complete test
suite through the final clean-installed DebugBrief wheel. All four commands
exited 0; mypy checked 145 source files; pytest reported **1,144 passed, 8 skipped,
1 dependency deprecation warning**, with zero failures/errors.

The skips are four opt-in Arena Docker integration checks, one user-site
interpreter condition, one actual Windows check, and two filesystem-specific
checks. These are not claimed as passes. DebugBrief's own Unix matrix suite has
no skipped tests in the passing rows.

Generated Markdown, HTML, and JSON, closed the session, and verified byte-for-byte
report copies. Rechecked the original project's Git status and every original
and copied source hash: unchanged. The report contains four actual checks, no
source changes, no current failures, and no manufactured failure-to-pass story.

Repeated all **12 deliberate resilience scenarios** through that installed wheel:
missing executable, timeout with descendant cleanup, interrupt/concurrent locks,
large output/terminal controls/synthetic secrets, failed pipeline, directory-aware
redo, broken output pipe, recorder hard crash/recovery, later regression,
malformed quoting, non-Git operation, and a symlinked state-directory refusal.
All passed their assertions. These deliberate probes are separate from the
normal Arena validation.

## Browser verification

Chromium: **72 passed**. Firefox: **66 passed**. WebKit: **66 passed**. The six
Chromium clipboard-permission checks are excluded from the other engines because
that permission API is not portable; native copy clicks and success feedback
were separately exercised in all three engines without mocking the clipboard.
Firefox's narrow checks use viewport reflow rather than mobile-browser emulation.
The repository preserves this coverage through `npm run test:site`,
`npm run test:site:firefox`, and `npm run test:site:webkit` after installing the
corresponding Playwright browser. The latter commands report six explicit skips
for the Chromium-only permission tests.

Each engine imported the exact real-project report and all **11 generated stress
reports**. Command counts, actual output, failure filters, directory context,
unresolved regression, and crash warnings matched the JSON. Import-only requests
were zero; no JavaScript errors occurred. Report and Explorer reflow were checked
at 375, 768, and 1440 CSS pixels. All local viewer links resolved.

WebKit emits one native-select style-policy warning even on a minimal page with
strict CSP and no application code or styles. The browser test accounts for
exactly that one warning and requires zero application inline styles; additional
violations/errors still fail. The strict production policy remains unchanged.
WebKit keyboard checks use its native Option-Tab behavior for links, consistent
with Apple's [WebKit keyboard preference](https://developer.apple.com/documentation/webkit/wkpreferences/tabfocuseslinks).

Nine fixture outputs match the current renderers. All local links, fragments,
ARIA references, and assets resolve. The `[db]` mark remains in every site header
and footer. README.md and the primary repository HEAD are unchanged.

## Native Safari follow-up

The later [Safari and accessibility review](SAFARI_ACCESSIBILITY_VERIFICATION.md)
records actual Safari 27.0.1 import, keyboard, search, filter, zoom and print-preview
checks. It also records the control-contrast and heading fixes and the revised
78/72/72 browser results. VoiceOver reading and live-announcement verification
remain incomplete; VoiceOver was verified off after the attempt.

## Remaining limits

- Deliberately detached processes are not a supported cleanup guarantee.
- Redaction does not prove that arbitrary secrets are absent; inspect exports.
- Physical Intel Mac, physical x86-64 Linux and physical iOS devices remain
  untested. Actual Safari on this ARM64 Mac is covered by the follow-up above.
  VoiceOver spoken output/live announcements remain unverified; native
  accessibility-tree inspection is not a completed screen-reader audit.
- No remote CI, public deployment, package publication, or extended independent
  field beta was performed. Windows remains unsupported by design.

## Evidence

All run artifacts are in ignored `artifacts/platform-verification-20261015/`:

- `final-verification.json`: final matrix, source parity, real-project, browser,
  and repository-invariant assertions.
- `macos/<version>/`: full JUnit/logs, build products, final row results, and clean
  installed-wheel evidence; packaging continuation preserves the preceding
  harness failure separately.
- `linux-arm64/`, `linux-amd64/`: Linux row logs and results.
- `Dockerfile`, `Dockerfile.amd64`, `matrix.py`, `linux-input/matrix-amd64.py`:
  reproducible local runtime and matrix setup.
- `real-final/source-manifest.json`, `verification.json`, `command-results.json`,
  `arena-results.xml`: real project identity, source invariants, exits and counts.
- `real-final/viewer/`: exact final report formats, real CLI transcript, recovered
  crash report, directory-aware report, and unresolved-regression report.
- `real-final/stress-results.json`: all twelve deliberate scenarios.
- `real-final/*-browser-verification.json` and screenshots: imports and actual UI.
- `chromium-final.log`, `firefox-final.log`, `webkit-final.log`: browser results.

The local transcript viewer is a demonstration aid showing actual subprocess
output, not a native terminal recording or part of the shipped product.
