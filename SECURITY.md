# Security

DebugBrief is a local-first command-line tool. It runs as you, on your machine,
and makes no network requests of its own.

## What data leaves your machine

DebugBrief itself sends nothing. It has no network code: no telemetry, no
accounts, no cloud sync, no update checks. It stores session state and reports under your project's `.debugbrief/`
directory. It also reads project configuration, Git metadata and changed-file
contents for comparison, and can add a local ignore entry in `.git/info/exclude`.
It runs local Git and the commands you ask it to capture.

Commands you run through DebugBrief keep their normal behavior. A command can
access the network exactly as it would if you ran it directly, so what such a
command sends is up to that command, not DebugBrief. Generated reports are plain
files; sharing one (pasting it into a pull request, for example) is an explicit
action you take.

## Threat model

The threat model is local. DebugBrief assumes the user running it and the files
it reads belong to that user. It is hardened against a few concrete local
hazards rather than a remote attacker:

- **Symlinked state.** DebugBrief refuses to follow a symlink (or a FIFO,
  socket, device, or non-directory) where it expects its own state: the
  `.debugbrief/` tree, the session and report files inside it, the active-session
  pointer, the active-command lease, and the lock files. It detects these with
  `lstat`/`O_NOFOLLOW` and a post-open `fstat`, so a planted link cannot redirect
  a read or write outside the project, and a special file cannot make it block.
  Unsafe entries are reported, never followed; one bad historical file does not
  break access to the others.
- **Restrictive permissions.** The `.debugbrief/` directories are created `0700`
  and session/report files `0600`, so other local accounts cannot read your
  debugging history under a permissive umask. This is best effort on filesystems
  that do not support Unix permissions.
- **Crash safety.** Session and report writes are atomic (write to a temp file,
  then rename). A running command holds an OS lock for its whole lifetime; if the
  recorder dies, an independent supervisor stops the owned command process
  group, including after SIGKILL. Cleanup releases the lock after its holders
  exit. `recover` preserves saved events and reports the lost result as unknown;
  it never turns an incomplete capture into a pass. Recovery can still report a
  held lock during cleanup, and deliberately detached processes can outlive it.
- **Repository-supplied state.** `.debugbrief/` lives inside the project, so a
  cloned repository could ship a pre-seeded session. DebugBrief does not trust
  that state to drive execution: `debugbrief redo` refuses to re-run a command
  from a session file that is tracked by Git (the state DebugBrief writes is kept
  out of the index). This check **fails closed**: when a Git repository is
  present but DebugBrief cannot confirm whether the session file is tracked (Git
  is unavailable or times out, or the repository is rejected, for example a
  `safe.directory` ownership error, or is corrupt), `redo` refuses rather than
  assume the state is safe. In a directory that is not a Git repository at all,
  no repository could have supplied the state, so `redo` still works normally,
  including on machines without Git installed. The id inside a session file is
  bound to its filename so it cannot redirect a write outside `sessions/`, and a
  malformed or hostile state file produces a reported error rather than a crash.

It does **not** defend against a local attacker who already has write access to
your `.debugbrief/` directory or who can modify the commands you run.

## Foreground commands and cleanup

Capture is intended for commands that finish, not for managing long-lived servers.
On normal completion, timeout, interruption or recorder death, the supervisor
stops ordinary descendants in its process group. It escalates from SIGTERM to
SIGKILL when needed. A program that deliberately creates another session or
process group can survive. If it also closes its captured streams, its continued
execution may be undetectable to the recorder.

A surviving process can retain the command lock. `status` and `recover` report
that the lock is still held; recovery will not clear it to pretend the project
is idle. After cleanup finishes, retry recovery. Saved events remain available,
but a crash can lose the command result that had not yet been recorded.

## Reports and the browser Explorer

The project-root field shows the repository name by default. The
`--include-full-path` flag exposes the full root intentionally. This setting does
not remove absolute paths inside command text, output or notes.

Generated HTML reports escape recorded text, include their own styles and use a
restrictive Content Security Policy. They contain no executable application
scripts or external assets.

The static website is separate from the CLI. It loads fonts and optional homepage
animation libraries from a CDN. Its Explorer reads one JSON report in memory,
without uploading it or saving report contents to browser storage. Only an
explicitly chosen theme is saved. File size, structure and list lengths are
bounded; the current limits include 5 MiB per file and 2,000 entries per list.
Imported text is rendered as text, not HTML or executable commands. A fixed
allowlist supplies the bundled examples. The Explorer displays the report's
claims; it does not authenticate or rerun the recorded checks.

## Redaction is best effort

Before anything is written to disk, DebugBrief scrubs common secret shapes from
command text, captured output, notes, titles, warnings, the session pointer, and
the lease: sensitive `name=value` and quoted/JSON `"name": "value"` pairs,
`Authorization`/`Bearer` headers, OpenAI/AWS/GitHub-style keys,
connection-string passwords, and PEM private-key blocks, each replaced with
`[redacted]`. Pass `--no-redact` to store raw command text and captured output
for that command. Redaction applies to stored text, not to the command's live
terminal output.

This is **best effort and conservative; it is not a guarantee.** It recognizes
known shapes and will miss secrets that do not match one. Known limitations
include space-separated flag values (`--token abc`) and a secret embedded in a
backslash-escaped string literal inside a command. Review a report before you
share it, and prefer not to print real secrets through DebugBrief in the first
place.

## Supported platforms

Unix-like systems only. Linux and macOS are tested in CI across Python 3.9
through 3.14. Other Unix-like systems may work but are not currently tested.
Native Windows and PowerShell are not supported.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue. Use
the repository's [private vulnerability report form](https://github.com/hari-kancharla/Debug-Brief/security/advisories/new).
Include the affected version, steps to reproduce and the impact. Remove unrelated
secrets and personal data from logs. The maintainer can follow up privately.
