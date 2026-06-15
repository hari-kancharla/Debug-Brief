# Security

DebugBrief is a local-first command-line tool. It runs as you, on your machine,
and sends nothing over the network.

## What data leaves your machine

Nothing. DebugBrief has no network code: no telemetry, no accounts, no cloud
sync, no update checks. It reads and writes only under your project's
`.debugbrief/` directory and shells out to local `git` and to the commands you
ask it to run. Generated reports are plain files; sharing one (pasting it into a
pull request, for example) is an explicit action you take.

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
  process dies, the lock is released by the OS and `debugbrief recover` reports
  the unfinished command without losing the session.
- **Repository-supplied state.** `.debugbrief/` lives inside the project, so a
  cloned repository could ship a pre-seeded session. DebugBrief does not trust
  that state to drive execution: `debugbrief redo` refuses to re-run a command
  from a session file that is tracked by Git (the state DebugBrief writes is kept
  out of the index), the id inside a session file is bound to its filename so it
  cannot redirect a write outside `sessions/`, and a malformed or hostile state
  file produces a reported error rather than a crash.

It does **not** defend against a local attacker who already has write access to
your `.debugbrief/` directory or who can modify the commands you run.

## Command capture is foreground-only

DebugBrief captures a single foreground command and holds the active-command
lease until that command's process tree exits. If a captured command backgrounds
a long-lived process (for example `debugbrief run --shell "my-server &"`), that
descendant keeps the lease held until it exits, so a later `run` may report that
a command is still running. When that happens, `debugbrief status` and
`debugbrief recover` detect and explain the held lock rather than reporting the
project as idle. Capture, and the lease, are designed for foreground commands;
start long-running daemons outside DebugBrief.

## Redaction is best effort

Before anything is written to disk, DebugBrief scrubs common secret shapes from
command text, captured output, notes, titles, warnings, the session pointer, and
the lease: sensitive `name=value` and quoted/JSON `"name": "value"` pairs,
`Authorization`/`Bearer` headers, OpenAI/AWS/GitHub-style keys,
connection-string passwords, and PEM private-key blocks, each replaced with
`[redacted]`. Pass `--no-redact` to store a command's output verbatim.

This is **best effort and conservative; it is not a guarantee.** It recognizes
known shapes and will miss secrets that do not match one. Known limitations
include space-separated flag values (`--token abc`) and a secret embedded in a
backslash-escaped string literal inside a command. Review a report before you
share it, and prefer not to print real secrets through DebugBrief in the first
place.

## Supported platforms

Unix-like systems only. Linux and macOS are tested in CI across Python
3.9-3.14. BSD should work. Native Windows and PowerShell are not supported.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue. Use
GitHub's "Report a vulnerability" (Security advisories) for this repository, or
contact the maintainer through the address on their GitHub profile. Include
steps to reproduce and the affected version; you will get an acknowledgement and
a fix timeline.
