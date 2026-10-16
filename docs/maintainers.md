# Maintainer notes

The settings below were checked on 2026-10-16. GitHub settings are separate from
the source tree; verify the live configuration before changing protections or
publishing a release.

## Current main protection

The active `Protect main` ruleset targets the default branch. It requires a pull
request, resolved review conversations and the configured status checks. It
blocks force pushes and branch deletion. No bypass actors are configured.

Required approvals are zero for the single-maintainer repository. Code-owner
review and linear history are not required, and branches do not have to be
up to date before merging. Do not describe these as enabled protections.

The required checks currently are:

- `lint`
- `test (macos-latest, 3.9)`
- `test (macos-latest, 3.10)`
- `test (macos-latest, 3.11)`
- `test (macos-latest, 3.12)`
- `test (macos-latest, 3.13)`
- `test (macos-latest, 3.14)`
- `test (ubuntu-latest, 3.10)`
- `test (ubuntu-latest, 3.11)`
- `test (ubuntu-latest, 3.12)`
- `test (ubuntu-latest, 3.13)`

CI also runs Ubuntu Python 3.9 and 3.14, plus the separate `website` check.
Those jobs are not required by the current ruleset. Before a release, wait for
all 13 CI jobs and the website job to pass, not just the enforced subset. Verify
the checks belong to the exact commit being merged, then check the new main runs
as well. Outside contributions are reviewed by the owner before merging.

Private vulnerability reporting is enabled. The private report form linked from
`SECURITY.md` is the intended channel for security issues, not public issues.

## PyPI publishing

The existing `debugbrief` project trusts the GitHub repository
`hari-kancharla/Debug-Brief`, workflow `release.yml`, environment `pypi`.
The workflow uses short-lived OpenID Connect credentials; it does not need a
stored `PYPI_API_TOKEN` secret. Only the publishing job receives `id-token: write`.
A separate job builds and checks the wheel and source archive, then the publishing
job downloads those artifacts and uploads them.

The `pypi` environment allows tags matching `v*`, not branch deployments. This
restricts which refs can use the environment. It does not make tags immutable:
there is currently no separate tag-protection ruleset. Protecting release tags
against updates and deletion is a recommended additional setting, not an enabled
one.

See the official [PyPI setup guide](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
and [security considerations](https://docs.pypi.org/trusted-publishers/security-model/)
when changing the publisher. Changing a repository owner, workflow filename or
environment requires checking both the workflow and the PyPI configuration.

## Release procedure

1. Update the version in `pyproject.toml` and `src/debugbrief/__init__.py`, and add
   the dated changelog entry. Review the README, command reference, security
   policy and install examples against the actual behavior.
2. Run the tests, fixture/link checks and package build. Install the built wheel
   in a clean environment and exercise capture, replay and report generation.
3. Open a pull request. Wait for every CI and website job, inspect failures, then
   merge the tested revision into main. Delete the temporary branch and verify
   the local checkout matches remote main.
4. Publish a GitHub Release with a version tag pointing to that main commit. The
   workflow starts on a published release, not on a plain push or tag creation.
   It refuses a tag that disagrees with the package version.
5. Watch both release jobs through completion. If publishing fails, read the log
   and check PyPI before retrying; a partial upload is not a completed release.
6. Verify the public PyPI version, wheel and source hashes against the build
   artifacts. Install the version from official PyPI in another clean environment,
   run the CLI checks, and verify the public README renders correctly.

Do not move a published version tag or overwrite release files. PyPI retains the
metadata submitted for a version, so a corrected packaged README needs a new
release. Keep historical failed attempts visible; confirm the current retry and
current main checks actually passed.

When another trusted maintainer joins, review approval requirements, code owners,
required checks and release permissions together. Avoid changing protections
without checking that maintainers can still complete the intended workflow.
