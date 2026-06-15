"""Session lifecycle and persistence.

The canonical live record for an active session is its file under
``.debugbrief/sessions/<id>.json``; it is rewritten immediately after every
event so a crash never loses captured work. ``active_session.json`` is a small
pointer to the currently-active session and is removed on a clean ``end``.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import git_utils
from .command_runner import RunResult
from .models import (
    NON_SUCCESS_STATUSES,
    CommandData,
    Event,
    FileChange,
    Session,
    SessionStatus,
)
from .paths import ProjectPaths, UnsafeStateDirectory, is_valid_session_id
from .redaction import redact_text
from .utils import (
    atomic_write_json,
    is_regular_file,
    now_iso8601,
    read_json_safe,
)


class SessionError(Exception):
    """Raised for expected, user-facing session errors."""


def _require_regular_or_absent(path: "Any", label: str) -> None:
    """Reject a lock/state file that exists but is not a regular file.

    A symlink, FIFO, socket, or device at a lock path could redirect state or
    block ``os.open``; ``O_NOFOLLOW`` alone catches only symlinks.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return  # absent; it will be created as a regular file
    except OSError:
        return  # a concrete error will surface on use
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeStateDirectory(
            f"{label} ({path}) is not a regular file; DebugBrief refuses to use "
            "it. Remove it to recover."
        )


def _open_lock_fd(path: "Any", label: str) -> int:
    """Open (creating if absent) a lock file, refusing a non-regular target.

    Mirrors the safe state-file reader: ``O_NOFOLLOW`` refuses a symlink at the
    final component, ``O_NONBLOCK`` avoids blocking on a planted FIFO, and a
    post-open ``fstat`` closes the race a separate ``lstat`` leaves, so a path
    swapped to a symlink or FIFO between the check and the open is still rejected.
    Returns an fd the caller owns and must close.
    """
    _require_regular_or_absent(path, label)
    flags = (
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(str(path), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafeStateDirectory(
                f"{label} ({path}) is not a regular file; DebugBrief refuses to "
                "use it. Remove it to recover."
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


class SessionManager:
    def __init__(self, paths: ProjectPaths) -> None:
        self.paths = paths
        # While a command runs, the command-lock fd, so the captured child can
        # inherit it and keep the lock held even if this parent is killed.
        self._command_lock_fd: Optional[int] = None

    @property
    def command_pass_fds(self) -> "tuple[int, ...]":
        """Fds to pass to a captured command (the held command lock, if any).

        Passing the command-lock fd to the child means the flock is held until
        the child exits, not only while this parent lives, so a parent killed
        mid-command does not let the lease look stale while the child runs on.
        """
        return (self._command_lock_fd,) if self._command_lock_fd is not None else ()

    @contextlib.contextmanager
    def _repo_lock(self) -> Iterator[None]:
        """Serialize a session's read-modify-write across concurrent processes.

        Two terminals finishing commands at nearly the same time would otherwise
        both load the session, append an event, and save, and the second writer
        would clobber the first writer's event. An exclusive advisory lock on a
        per-repository lock file makes the load-append-save atomic. The lock is
        held only around that quick persistence step, never while a command runs.
        """
        self.paths.assert_state_dirs_safe()
        self.paths.base_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.paths.base_dir / ".lock"
        # The lock must be a regular file or absent: a symlink could redirect it
        # and a FIFO could block os.open. _open_lock_fd opens with O_NOFOLLOW and
        # O_NONBLOCK and confirms the opened descriptor with fstat, closing the
        # race a separate lstat would leave.
        fd = _open_lock_fd(lock_path, ".debugbrief/.lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # Active-command lease ----------------------------------------------------
    # A running command holds an exclusive flock on .debugbrief/.command.lock for
    # its entire lifetime, alongside readable metadata in active_command.json. The
    # OS releases the flock automatically if the process dies, so a crash never
    # wedges the lease and stale detection needs no PID probing.
    def _open_command_lock(self) -> int:
        # O_NOFOLLOW + O_NONBLOCK + a post-open fstat (see _open_lock_fd) reject a
        # symlink/FIFO planted at the lock path even if it is swapped in after the
        # lstat, closing the same race already closed for state-file reads.
        return _open_lock_fd(
            self.paths.command_lock_file, ".debugbrief/.command.lock"
        )

    def _command_is_active(self) -> bool:
        """True only if a live process currently holds the command lock."""
        lock_path = self.paths.command_lock_file
        try:
            info = os.lstat(lock_path)
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode):
            return False  # not a real lock file; _open_command_lock will reject it
        # O_NOFOLLOW + O_NONBLOCK and a post-open fstat: a path swapped to a
        # symlink or FIFO after the lstat is rejected rather than followed or
        # blocked on, matching the safe open used to acquire the lock.
        try:
            fd = os.open(
                str(lock_path),
                os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
        except OSError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False  # swapped to a non-regular file; not a live lock
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True  # another live process holds it
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def _write_command_lease(
        self, session: Session, command_id: str, preview: str, invocation_cwd: str
    ) -> None:
        redacted_preview, _ = redact_text(preview or "")
        atomic_write_json(
            self.paths.active_command_file,
            {
                "schema_version": 1,
                "session_id": session.session_id,
                "command_id": command_id,
                "command_preview": redacted_preview[:200],
                "invocation_cwd": str(invocation_cwd),
                "pid": os.getpid(),
                "started_at": now_iso8601(),
            },
        )

    def _clear_command_lease(self) -> bool:
        """Remove the lease file; return True if it is gone afterward.

        Also removes an (empty) directory planted at the lease path. A non-empty
        directory or other stubborn entry cannot be removed and returns False, so
        callers report an actionable error instead of claiming success.
        """
        path = self.paths.active_command_file
        with contextlib.suppress(OSError):
            path.unlink()
        with contextlib.suppress(OSError):
            os.rmdir(path)
        # lexists, not exists: a dangling symlink that could not be removed is
        # still an entry at this path, so cleanup must not report success while it
        # remains (exists() is False for a broken symlink and would lie here).
        return not os.path.lexists(path)

    def _clear_command_lease_if(self, command_id: str) -> None:
        """Remove the lease only when it belongs to ``command_id``.

        Called after a command's result is persisted, so the lease is cleared
        only once the event is safely on disk. If persistence failed, the lease
        is left behind for ``recover``.
        """
        try:
            meta = read_json_safe(self.paths.active_command_file)
        except (ValueError, OSError):
            return
        if isinstance(meta, dict) and meta.get("command_id") == command_id:
            self._clear_command_lease()

    def _lease_session_id(self, command_id: str) -> Optional[str]:
        """Return the session id the lease for ``command_id`` belongs to, or None.

        A command's result must be recorded against the session that started it,
        even if the active pointer changed while the command ran.
        """
        try:
            meta = read_json_safe(self.paths.active_command_file)
        except (ValueError, OSError):
            return None
        if isinstance(meta, dict) and meta.get("command_id") == command_id:
            sid = meta.get("session_id")
            return sid if is_valid_session_id(sid) else None
        return None

    @staticmethod
    def _command_recorded(session: Session, command_id: str) -> bool:
        """True if a command event with this id is already on the session.

        Reads the raw command_id field rather than deserializing each stored
        command. An older event can carry malformed optional data (e.g. a null
        list); deserializing it just to compare an id would raise after the
        just-finished command completed, stranding its result and leaving the
        lease behind. event.data is always a dict (see Event.from_dict).
        """
        return any(
            event.data.get("command_id") == command_id
            for event in session.command_events()
        )

    def _reap_stale_lease(self) -> None:
        """Recover a stale lease before a new lease, end, or cancel.

        A lease whose lock is no longer held (its process crashed) is recovered
        in place: a warning is added if its command never recorded, and the
        metadata is cleared. A live lease is left untouched. Called under the
        repo lock so the decision and recovery are atomic.
        """
        if not os.path.lexists(self.paths.active_command_file):
            return  # lexists: a dangling lease symlink must still be reaped
        if self._command_is_active():
            return
        self._recover_stale_lease()

    def _recover_stale_lease(self) -> None:
        """Clear a stale lease, warning only if its command never recorded.

        The owning process is gone. If its result was already persisted (the
        lease's command_id is present among the session's events), the process
        simply died after recording but before clearing the lease, so there is
        nothing to warn about. Otherwise the command was lost and a (redacted)
        warning is added so the report admits the gap. Session data is kept
        either way. Called under the repo lock.
        """
        lease_path = self.paths.active_command_file
        if not is_regular_file(lease_path):
            self._clear_command_lease()  # symlink/FIFO/etc: not a real lease
            return
        try:
            meta = read_json_safe(lease_path)
        except (ValueError, OSError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        session_id = meta.get("session_id")
        command_id = meta.get("command_id")
        preview = meta.get("command_preview", "")
        if (
            isinstance(session_id, str)
            and is_valid_session_id(session_id)
            and self.paths.session_file(session_id).exists()
        ):
            with contextlib.suppress(SessionError):
                session = self.load_session_file(session_id)
                already_recorded = command_id is not None and self._command_recorded(
                    session, command_id
                )
                if not already_recorded:
                    # add_warning redacts, so even an unredacted preview is safe.
                    session.add_warning(
                        "A captured command did not finish (its process ended "
                        f"before recording a result): {preview}".strip(),
                        now_iso8601(),
                    )
                    self.save_session(session)
        self._clear_command_lease()

    @contextlib.contextmanager
    def command_lease(
        self, command_preview: str, invocation_cwd: str
    ) -> Iterator[str]:
        """Hold an exclusive command lease for the lifetime of one command.

        Under the repo lock it first recovers any stale lease, refuses a second
        concurrent command, takes the command lock (held until the command
        finishes, so ``end``/``cancel``/a second ``run`` see it), and writes the
        readable lease. The repo lock is released while the command runs. On exit
        this only releases the OS lock; the lease metadata is cleared by
        ``record_command`` after the result is persisted, so a failed persistence
        leaves the lease for ``recover`` rather than erasing the evidence. Yields
        a unique command id so the result is appended exactly once.
        """
        command_id = uuid.uuid4().hex
        with self._repo_lock():
            self._reap_stale_lease()
            # If a non-regular lease (e.g. a directory) could not be reaped, refuse
            # with a clear message rather than crash when atomic_write_json later
            # tries to replace it.
            _require_regular_or_absent(
                self.paths.active_command_file, ".debugbrief/active_command.json"
            )
            session = self.require_active("run a command")
            lock_fd = self._open_command_lock()
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(lock_fd)
                raise SessionError(
                    "A captured command is already running in this project. Wait "
                    "for it to finish before running another."
                ) from exc
            try:
                self._write_command_lease(
                    session, command_id, command_preview, invocation_cwd
                )
            except BaseException:
                # Writing the lease failed (e.g. disk full) after the lock was
                # taken; release it, or every later command is wrongly rejected as
                # already running. The yield-side cleanup below is never reached.
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
                raise
        # Expose the lock fd so the captured child can inherit it (see
        # command_pass_fds); the child then holds the lock until it exits.
        self._command_lock_fd = lock_fd
        clean_exit = False
        try:
            yield command_id
            clean_exit = True
        finally:
            self._command_lock_fd = None
            # On a clean exit, clear the lease for this command: if a result was
            # recorded, record_command already cleared it; if the body returned
            # early without running (e.g. redo of a redacted command), clear it so
            # it is not mistaken for a crashed command. On an exception (a failed
            # persistence), leave the lease so recover can report the lost result.
            if clean_exit:
                with contextlib.suppress(Exception), self._repo_lock():
                    self._clear_command_lease_if(command_id)
            with contextlib.suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    # Active-pointer handling -------------------------------------------------
    def _read_active_pointer(self) -> Optional[Dict[str, Any]]:
        # Validate the state directories before any read, so a symlinked
        # .debugbrief is refused on read-only commands too, not only mutations.
        self.paths.assert_state_dirs_safe()
        pointer_path = self.paths.active_session_file
        if not pointer_path.exists():
            return None
        # Refuse a symlinked or non-regular pointer rather than follow it.
        _require_regular_or_absent(pointer_path, ".debugbrief/active_session.json")
        try:
            data = read_json_safe(pointer_path)
        except (ValueError, OSError) as exc:
            raise SessionError(
                f"active_session.json exists but could not be read ({exc}). "
                "Inspect or remove .debugbrief/active_session.json to recover."
            ) from exc
        if not isinstance(data, dict) or not is_valid_session_id(data.get("session_id")):
            raise SessionError(
                "active_session.json is malformed. Remove "
                ".debugbrief/active_session.json to recover."
            )
        return data

    def _write_active_pointer(self, session: Session) -> None:
        atomic_write_json(
            self.paths.active_session_file,
            {
                "session_id": session.session_id,
                "title": session.title,
                "status": session.status,
                "started_at": session.timestamps.start,
                "session_file": str(
                    self.paths.session_file(session.session_id)
                ),
            },
        )

    def _clear_active_pointer(self) -> None:
        pointer_path = self.paths.active_session_file
        try:
            if pointer_path.exists():
                pointer_path.unlink()
        except OSError as exc:  # pragma: no cover - defensive
            raise SessionError(
                f"Could not clear active_session.json ({exc}). Remove it manually."
            ) from exc

    def has_active(self) -> bool:
        return self.paths.active_session_file.exists()

    # Session persistence -----------------------------------------------------
    def save_session(self, session: Session) -> None:
        # The id becomes a file path under sessions/, so never write through an
        # unvalidated one: a corrupt or hostile id (e.g. one carrying ../) must be
        # refused at the write boundary, not allowed to escape the directory.
        if not is_valid_session_id(session.session_id):
            raise SessionError(
                f"Refusing to save a session with an invalid id "
                f"({session.session_id!r})."
            )
        self._recompute_counts(session)
        atomic_write_json(
            self.paths.session_file(session.session_id), session.to_dict()
        )

    def load_session_file(self, session_id: str) -> Session:
        self.paths.assert_state_dirs_safe()
        if not is_valid_session_id(session_id):
            raise SessionError(f"Invalid session id {session_id!r}.")
        path = self.paths.session_file(session_id)
        if not is_regular_file(path):
            if path.exists():
                raise SessionError(
                    f"Session file for {session_id} is not a regular file; "
                    "DebugBrief refuses to follow it."
                )
            raise SessionError(f"Session file not found for id {session_id}.")
        try:
            # read_json_safe re-checks with O_NOFOLLOW, closing the lstat race.
            session = Session.from_dict(read_json_safe(path))
        except (ValueError, TypeError, OSError) as exc:
            raise SessionError(f"Could not read session {session_id}: {exc}") from exc
        # Bind the loaded object to the file it was read from. The session_id
        # embedded in the JSON is otherwise used to build write paths
        # (save_session, report_file), so a file whose body carries a different or
        # path-traversing id must be refused, never trusted. The requested id was
        # validated above, so requiring equality keeps every later write inside
        # sessions/.
        if session.session_id != session_id:
            raise SessionError(
                f"Session file {session_id} declares a different id "
                f"({session.session_id!r}); refusing to use it."
            )
        return session

    def load_active(self) -> Optional[Session]:
        """Return the active Session, or None if no session is active.

        Raises SessionError if the pointer exists but the underlying session
        file is missing/unreadable (an interrupted/inconsistent state).
        """
        pointer = self._read_active_pointer()
        if pointer is None:
            return None
        session_id = pointer["session_id"]
        path = self.paths.session_file(session_id)
        if not path.exists():
            raise SessionError(
                "active_session.json points to a missing session file "
                f"({session_id}). The session looks interrupted. Remove "
                ".debugbrief/active_session.json to recover."
            )
        return self.load_session_file(session_id)

    def require_active(self, action: str) -> Session:
        session = self.load_active()
        if session is None:
            raise SessionError(
                f"No active DebugBrief session. Cannot {action}. "
                'Start one with: debugbrief start "<title>"'
            )
        return session

    # Lifecycle ---------------------------------------------------------------
    def start(self, title: str) -> Session:
        clean_title = title.strip()
        if not clean_title:
            raise SessionError("Session title must not be empty.")
        # The title can come straight from a command line (auto-start seeds it
        # from the raw command), so scrub secrets before it is persisted to the
        # session file and the active pointer, the same as command output.
        clean_title, _ = redact_text(clean_title)

        # Decide and create under the repo lock so two simultaneous starts cannot
        # both pass the "already active" check and create two active sessions.
        with self._repo_lock():
            # Refuse to start while a command is running: its result will be
            # recorded against its own session, and a new session must not appear
            # under it (which could otherwise capture that result).
            self._reap_stale_lease()
            if self._command_is_active():
                raise SessionError(
                    "A captured command is still running in this project; wait for "
                    "it to finish before starting a new session."
                )
            if self.has_active():
                existing = self._read_active_pointer() or {}
                raise SessionError(
                    "A DebugBrief session is already active"
                    + (f" ({existing.get('title')!r})." if existing.get("title") else ".")
                    + " End it with: debugbrief end --mode pr|handoff|incident, "
                    "or check it with: debugbrief status"
                )

            self.paths.ensure_directories()
            git_state = git_utils.capture_state(self.paths.project_root, initial=True)
            if git_state.is_repo:
                # Baseline of files already changed before the session, so the
                # final report counts only what the session actually changed.
                git_state.initial_dirty = git_utils.working_tree_fingerprints(
                    self.paths.project_root
                )

            session = Session(
                title=clean_title,
                project_root=str(self.paths.project_root),
                git=git_state,
            )
            session.timestamps.start = now_iso8601()

            # Record an initial snapshot event for an honest timeline.
            session.events.append(
                Event.snapshot(
                    {
                        "phase": "start",
                        "git": git_state.to_dict(),
                    },
                    session.timestamps.start,
                )
            )

            self.save_session(session)
            self._write_active_pointer(session)
            return session

    def auto_start(self, seed_text: str) -> Session:
        """Start a session with a title derived from the time and ``seed_text``.

        Used when ``run`` or ``note`` is invoked with no active session, so a
        capture is never silently dropped.
        """
        from .utils import utc_now

        first_line = ""
        for line in (seed_text or "").strip().splitlines():
            if line.strip():
                first_line = line.strip()
                break
        # Redact the full line before truncating. Truncation can cut the syntax a
        # redaction pattern needs (the "@host" of a connection string, say) while
        # keeping the secret, so redacting only the 60-char snippet could miss it.
        # start() redacts again, harmlessly, and also covers manual titles.
        if first_line:
            first_line, _ = redact_text(first_line)
        snippet = first_line[:60] if first_line else "debug session"
        stamp = utc_now().strftime("%Y-%m-%d %H:%M")
        return self.start(f"Auto session {stamp}: {snippet}")

    def add_note(self, text: str) -> Session:
        clean = text.strip()
        if not clean:
            raise SessionError("Note text must not be empty.")
        # Notes are persisted to the session JSON and surfaced in reports, so a
        # secret pasted into a note (an env var, a log line) must be scrubbed
        # before it ever reaches disk, the same as captured command output.
        clean, n_redacted = redact_text(clean)
        with self._repo_lock():
            session = self.require_active("add a note")
            note_event = Event.note(clean, now_iso8601())
            if n_redacted:
                note_event.data["redacted"] = True
            session.events.append(note_event)
            self.save_session(session)
            self._write_active_pointer(session)
            return session

    def record_command(
        self, result: RunResult, command_id: Optional[str] = None
    ) -> Session:
        # Lightweight git snapshot at the moment of the command, taken outside
        # the lock since it only reads the working tree, so reports can later
        # correlate file changes. Safe and silent outside a repo.
        if self.paths.is_git_repo:
            cwd = self.paths.project_root
            result.command_data.git_head = git_utils.current_short_sha(cwd)
            result.command_data.git_changed_files = git_utils.changed_files(cwd)
        result.command_data.command_id = command_id
        with self._repo_lock():
            # Record against the session that owns the lease, not whatever is
            # active now: the active pointer may have changed while the command
            # ran, and the result must never land in a different session.
            target_id = self._lease_session_id(command_id) if command_id else None
            if target_id is not None:
                session = self.load_session_file(target_id)
            else:
                session = self.require_active("run a command")
            # Idempotent on command_id: a retried persistence after a partial
            # failure must not append the same result twice. The result is
            # already on disk, so the lease can be cleared.
            if command_id is not None and self._command_recorded(session, command_id):
                self._clear_command_lease_if(command_id)
                return session
            session.events.append(
                Event.command(result.command_data, result.command_data.started_at)
            )
            # add_warning redacts the text, so error/warning messages that echo
            # the command or its output are scrubbed before they reach disk.
            if result.error_message and (
                result.errored or result.timed_out or result.interrupted
            ):
                session.add_warning(result.error_message, now_iso8601())
            if result.warning:
                session.add_warning(result.warning, now_iso8601())
            self.save_session(session)
            # Refresh the active pointer only if it still points at this session,
            # so recording an old command does not clobber a newer active session.
            pointer = self._read_active_pointer()
            if pointer is not None and pointer.get("session_id") == session.session_id:
                self._write_active_pointer(session)
            # Clear the lease only now that the event is safely persisted; if
            # save_session above raised, the lease is left for recover.
            if command_id is not None:
                self._clear_command_lease_if(command_id)
            return session

    def end(self, mode: str, report_format: str = "md", detail: str = "full") -> Session:
        # Local imports avoid an import cycle with the reporters package.
        import json

        from .reporters import render_report, render_report_json
        from .utils import write_text

        with self._repo_lock():
            self._reap_stale_lease()
            if self._command_is_active():
                raise SessionError(
                    "A captured command is still running; wait for it to finish "
                    "before ending the session."
                )
            session = self.require_active("end the session")

            # Capture final Git state, preserving the initial SHA.
            final_state = git_utils.capture_state(
                self.paths.project_root, initial=False
            )
            session.git.final_sha = final_state.final_sha
            session.git.branch = final_state.branch
            session.git.detached_head = final_state.detached_head
            session.git.is_repo = final_state.is_repo
            if final_state.repo_root:
                session.git.repo_root = final_state.repo_root

            session.timestamps.end = now_iso8601()
            session.status = SessionStatus.COMPLETED.value
            session.events.append(
                Event.snapshot(
                    {"phase": "end", "git": session.git.to_dict()},
                    session.timestamps.end,
                )
            )
            self._finalize_summary(session)

            # Finalize transactionally: render every requested artifact first,
            # write each atomically, then persist the completed session, and
            # clear the active pointer last. A failure at any step leaves the
            # session active and recoverable, never completed without its report
            # or pointing at a half-written file.
            artifacts: List[Tuple[Any, str]] = []
            if report_format in ("md", "both"):
                artifacts.append(
                    (
                        self.paths.report_file(session.session_id, mode),
                        render_report(session, mode, detail),
                    )
                )
            if report_format in ("json", "both"):
                payload = render_report_json(session, mode)
                artifacts.append(
                    (
                        self.paths.report_json_file(session.session_id, mode),
                        json.dumps(payload, indent=2) + "\n",
                    )
                )
            for path, text in artifacts:
                write_text(path, text)

            self.save_session(session)
            self._clear_active_pointer()
            return session

    def preview(self, mode: str, detail: str = "full") -> str:
        """Render a report for the active session without mutating anything.

        Works on a deep copy (via the dict round trip), so the live session
        keeps its status, timestamps, and file exactly as they are; no report
        file is written. The summary is finalized on the copy only.
        """
        from .reporters import render_report  # local import avoids a cycle

        session = self.require_active("preview the report")
        copy = Session.from_dict(session.to_dict())
        self._finalize_summary(copy)
        return render_report(copy, mode, detail)

    def cancel(self) -> Session:
        """Discard the active session without writing a report.

        The session file is kept on disk with status ABANDONED, so nothing is
        silently deleted; it simply never becomes a brief.
        """
        with self._repo_lock():
            self._reap_stale_lease()
            if self._command_is_active():
                raise SessionError(
                    "A captured command is still running; wait for it to finish "
                    "before cancelling the session."
                )
            session = self.require_active("cancel the session")
            session.status = SessionStatus.ABANDONED.value
            session.timestamps.end = now_iso8601()
            self.save_session(session)
            self._clear_active_pointer()
            return session

    def recover(self) -> Dict[str, Any]:
        """Repair a broken or stale active-session pointer; report corrupt files.

        A healthy active session is left untouched. A pointer to a missing or
        unreadable session file, or to a session that is no longer active (a
        finalize interrupted before the pointer was cleared), is cleared so a new
        session can start. Corrupt historical session files are reported, never
        deleted, so nothing is lost silently.
        """
        result: Dict[str, Any] = {
            "action": "none", "detail": "", "corrupt": [], "lease": "none"
        }
        # With no state directory there is nothing to recover, and acquiring the
        # repo lock would create .debugbrief/.lock (under the umask, not 0700).
        # Use lexists, not exists: a dangling symlink at .debugbrief still exists
        # as a link and must be reported (assert_state_dirs_safe below rejects
        # it), not treated as "nothing to recover".
        if not os.path.lexists(self.paths.base_dir):
            return result
        with self._repo_lock():
            # Command lease first: a live one (lock still held by its process) is
            # left untouched; a stale one (owner gone) is warned about on its
            # session and cleared, never touching session data.
            if os.path.lexists(self.paths.active_command_file):
                if self._command_is_active():
                    result["lease"] = "live"
                else:
                    self._recover_stale_lease()
                    # Only claim cleared if the lease is actually gone; a stubborn
                    # entry (a non-empty directory, or a dangling symlink that
                    # could not be unlinked) is reported, not hidden. lexists, not
                    # exists, so a broken symlink still counts as present.
                    if os.path.lexists(self.paths.active_command_file):
                        result["lease"] = "unclearable"
                    else:
                        result["lease"] = "cleared_stale"

            if self.has_active():
                try:
                    session = self.load_active()
                except SessionError as exc:
                    self._clear_active_pointer()
                    result["action"] = "cleared_broken_pointer"
                    result["detail"] = str(exc)
                else:
                    if (
                        session is not None
                        and session.status == SessionStatus.ACTIVE.value
                    ):
                        result["action"] = "healthy"
                        result["detail"] = session.title
                    else:
                        self._clear_active_pointer()
                        result["action"] = "cleared_stale_pointer"
                        result["detail"] = session.status if session else "unknown"

        sessions_dir = self.paths.sessions_dir
        if sessions_dir.is_dir():
            for path in sorted(sessions_dir.glob("*.json")):
                # An unsafe (symlinked/special) entry is reported, never followed;
                # a corrupt regular file is reported too. Neither is deleted.
                if not is_regular_file(path):
                    result["corrupt"].append(path.name)
                    continue
                try:
                    Session.from_dict(read_json_safe(path))
                except (ValueError, OSError, TypeError):
                    result["corrupt"].append(path.name)
        return result

    # Status ------------------------------------------------------------------
    def build_status(self) -> Dict[str, Any]:
        """Return a structured status payload for the CLI to render."""
        pointer = self._read_active_pointer()
        if pointer is None:
            return {"active": False}

        session_id = pointer.get("session_id", "")
        path = self.paths.session_file(session_id)
        if not path.exists():
            return {
                "active": True,
                "interrupted": True,
                "session_id": session_id,
                "title": pointer.get("title"),
                "reason": "Session file is missing.",
            }

        session = self.load_session_file(session_id)
        self._recompute_counts(session)
        interrupted = session.status != SessionStatus.ACTIVE.value
        return {
            "active": True,
            "interrupted": interrupted,
            "session_id": session.session_id,
            "title": session.title,
            "status": session.status,
            "project_root": session.project_root,
            "start": session.timestamps.start,
            "notes_count": session.summary.notes_count,
            "commands_count": session.summary.commands_count,
            "failed_commands_count": session.summary.failed_commands_count,
            "branch": session.git.branch,
            "detached_head": session.git.detached_head,
            "is_repo": session.git.is_repo,
            "warnings": list(session.warnings),
        }

    # Internal helpers --------------------------------------------------------
    def _recompute_counts(self, session: Session) -> None:
        commands = session.command_events()
        notes = session.note_events()
        failed = 0
        for event in commands:
            status = (event.data.get("classification") or {}).get("status")
            if status in NON_SUCCESS_STATUSES:
                failed += 1
        session.summary.notes_count = len(notes)
        session.summary.commands_count = len(commands)
        session.summary.failed_commands_count = failed

    def _finalize_summary(self, session: Session) -> None:
        self._recompute_counts(session)

        tests_run: List[str] = []
        for event in session.command_events():
            data = CommandData.from_dict(event.data)
            if data.classification.is_test:
                tests_run.append(data.command)
        session.summary.tests_run = tests_run

        if session.git.is_repo:
            pairs, added, deleted = git_utils.session_changes(
                self.paths.project_root,
                session.git.initial_sha,
                session.git.initial_dirty,
            )
            session.summary.file_changes = [
                FileChange(status=label, path=path) for label, path in pairs
            ]
            session.summary.modified_files = [path for _label, path in pairs]
            session.summary.lines_added = added
            session.summary.lines_deleted = deleted
        else:
            session.summary.file_changes = []
            session.summary.modified_files = []
            session.summary.lines_added = 0
            session.summary.lines_deleted = 0

        # The explicit-run capture model captures exactly what was run through
        # DebugBrief; there is no silent gap to report.
        session.summary.command_capture_status = "full"
