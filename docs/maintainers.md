# Maintainer notes

Repository protection for `main` and for release tags. These are GitHub
settings, not code, so they are configured in the repository settings rather
than applied from this file. The exact configuration is recorded here so it can
be reviewed and reproduced.

## Branch ruleset: Protect main

Create a repository ruleset named `Protect main`, targeting the `main` branch,
with status Active and these rules:

- Require a pull request before merging.
- Require status checks to pass before merging, with "Require branches to be up
  to date before merging" enabled.
- Require conversation resolution before merging.
- Block force pushes.
- Restrict deletions (block branch deletion).
- Require linear history.
- Restrict who can push to matching branches, so normal contributors cannot push
  to `main` directly and must go through a pull request.

### Required status checks

These are the exact check names produced by the current CI workflow
(`.github/workflows/ci.yml`). All of them must be required:

- `lint`
- `test (ubuntu-latest, 3.9)`
- `test (ubuntu-latest, 3.10)`
- `test (ubuntu-latest, 3.11)`
- `test (ubuntu-latest, 3.12)`
- `test (ubuntu-latest, 3.13)`
- `test (ubuntu-latest, 3.14)`
- `test (macos-latest, 3.9)`
- `test (macos-latest, 3.10)`
- `test (macos-latest, 3.11)`
- `test (macos-latest, 3.12)`
- `test (macos-latest, 3.13)`
- `test (macos-latest, 3.14)`

If the CI matrix (operating systems or Python versions) changes, update this
list to match the new check names.

### Approvals and bypass

While there is a single maintainer, required pull request approvals are not
enabled, because a sole maintainer cannot approve their own pull request and
would be unable to merge. Repository administrator bypass stays enabled for the
same reason, so the maintainer is not locked out.

Outside contributor pull requests are reviewed manually by the owner before
merge (see [CODEOWNERS](../.github/CODEOWNERS) and the Review and merge section
of [CONTRIBUTING.md](../CONTRIBUTING.md)).

Once a second trusted maintainer is added, switch to requiring one approval,
dismiss stale approvals on new commits, and require review from code owners.

## Tag protection: v*

Protect release tags matching `v*` so a published version cannot be moved or
removed:

- Block tag deletion.
- Block tag updates after creation.
- Allow only repository administrators or the release workflow to create them.

Tags drive the release workflow (`.github/workflows/release.yml`), which
publishes to PyPI only when a GitHub Release is published.
