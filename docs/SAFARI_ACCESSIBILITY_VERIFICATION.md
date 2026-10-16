# Safari and accessibility review — 2026-10-15

Historical record from before the supplied ZIP site replaced the earlier layout.
Its browser results do not verify the current site; the maintained browser suite
in `tests/browser/site.spec.js` covers the current design.

Tested in the installed **Safari 27.0.1 (22625.1.29.11.28)** on this ARM64 Mac,
using the native application and macOS file dialog. Playwright WebKit results
below are separate engine coverage.

## Changes from this review

- Kept the `[db]` identity, warm palette, compact page margins and generated report
  previews. Headings use a consistent upright style following user feedback.
  Edited repeated slogans and verification language across the homepage,
  Explorer and documentation. Replaced the large repeated privacy pitch with one
  compact explanation linking to the full documentation.
- Strengthened search and filter boundaries. The old border was about 1.5:1 against
  its background; the new control border is at least 3.36:1 against the adjacent
  page and 3.67:1 against the input surface. Decorative rules stay lighter.
- Corrected Explorer headings: the page is H1, each active report section starts
  with H2, and expanded command output uses H3. Previously several sections began
  with H3 and output jumped to H4.
- Added browser regressions for control-boundary contrast and active-tab heading
  hierarchy, including expanded command output.

## Native Safari observations

| Check | Observation |
| --- | --- |
| Keyboard entry and skip link | Option-Tab exposes a visible skip link; Return bypasses header navigation and subsequent focus enters the main content. |
| Explorer tab keyboard controls | Down selects Commands; End selects Scope & limits; Home returns to Overview. Focus is visibly outlined. |
| Real project import | The native macOS picker opened the exact CodeReview Arena `report.json`: four commands, two notes and the actual recorded results. |
| Output search | Searching `1144 passed` returned the one recorded pytest attempt. |
| Native outcome filter | The native menu selected Unsuccessful attempts and displayed one failed attempt from the sample. |
| Keyboard command disclosure | Option-Tab followed by Space expanded a command and exposed the captured output. The self-contained real-project HTML report also expanded through the keyboard. |
| Invalid JSON | The native picker opened malformed JSON; the error explained the problem and kept the previous report and output. |
| Clear and reload | Clear disabled itself, emptied the report and returned control to the file button. Reload with the explicit sample URL loaded that sample again. |
| Copy | The actual Safari copy action displayed “Command copied.” This observation confirms feedback, not a separate clipboard-content read. |
| Documentation search | Slash focused search. An unmatched query displayed zero matches; Escape cleared it; `timeout` displayed two matching sections. |
| Native zoom | Safari’s page menu selected 200%. Inspected enlarged text and controls, then restored Actual Size. Automated narrow reflow checks supplement this visual observation. |
| Printing | Native Safari print preview paginated the real HTML report into five pages, including the captured-output section. The dialog was cancelled; no printer job was sent. |
| Final heading repair | Reopened the final Explorer and observed Command history at level 2 and expanded STDOUT at level 3 in Safari’s accessibility tree. |

## Screen-reader limitation

VoiceOver was activated during the attempt. Its rotor and spoken output were not
reliably observable through the available native control connection. **A completed
VoiceOver reading/announcement test is not claimed.** Static accessibility trees
and axe checks do not replace it. VoiceOver was turned off in System Settings;
the native `AX_VOICEOVER_ENABLED` switch was verified **off** after testing.
Caption-panel and cursor preferences were inspected but not changed.

## Automated verification

The final behavior-changing revision passed **78 Chromium checks**, **72 Firefox
checks** and **72 Playwright WebKit checks** across desktop, tablet and mobile.
Firefox and WebKit each explicitly skip six Chromium-specific clipboard-permission
checks. These include axe WCAG-tag checks, imports, injection resistance, links,
keyboard controls, reflow, reduced motion, printing, contrast and headings.
Subsequent documentation wording and social-preview changes were checked with
focused render/search tests and local link validation.

These are local verification results, not a WCAG conformance certification or a
physical iPhone/iPad test. Python runtime code did not change in this review;
the previous platform matrix remains documented in PLATFORM_VERIFICATION.md.

Evidence is kept locally in ignored `artifacts/safari-accessibility-20261015/`:
engine logs, result summaries, before-edit source copies and review screenshots.
Saved PNG review images are Chromium renders; native Safari observations are
recorded separately above and were inspected live.

## Research used for the copy edit

Reviewed the official [uv introduction](https://docs.astral.sh/uv/),
[HTTPie CLI page](https://httpie.io/cli) and
[pytest introduction](https://docs.pytest.org/en/stable/). The useful pattern was
a direct explanation followed by concrete commands and output. This informed
editing and information order; their appearance was not copied. Zellij's visual
direction was rejected following the user's feedback. This is a targeted design
review, not evidence that DebugBrief outperforms every competing product.

Manual review followed the checks described by
[W3C WAI](https://www.w3.org/WAI/test-evaluate/preliminary/), with control-boundary
contrast evaluated against
[WCAG 2.2 non-text contrast](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html).

Nothing was pushed or published. README.md and the repository HEAD were preserved.

The user's final direction is desktop-first visual review. Further mobile work
is deferred until the desktop appearance is accepted. The final upright heading
and documentation copy passed three focused Chromium desktop render/search
checks; earlier full engine results are retained separately. Existing release
archives were not regenerated for these site edits.
