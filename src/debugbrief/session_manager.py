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
from .paths import ProjectPaths
from .redaction import redact_text
from .utils import atomic_write_json, now_iso8601, read_json


class SessionError(Exception):
    """Raised for expected, user-facing session errors."""


class SessionManager:
    def __init__(self, paths: ProjectPaths) -> None:
        self.paths = paths

    @contextlib.contextmanager
    def _repo_lock(self) -> Iterator[None]:
        """Serialize a session's read-modify-write across concurrent processes.

        Two terminals finishing commands at nearly the same time would otherwise
        both load the session, append an event, and save, and the second writer
        would clobber the first writer's event. An exclusive advisory lock on a
        per-repository lock file makes the load-append-save atomic. The lock is
        held only around that quick persistence step, never while a command runs.
        """
        self.paths.base_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.paths.base_dir / ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # Active-pointer handling -------------------------------------------------
    def _read_active_pointer(self) -> Optional[Dict[str, Any]]:
        pointer_path = self.paths.active_session_file
        if not pointer_path.exists():
            return None
        try:
            data = read_json(pointer_path)
        except (ValueError, OSError) as exc:
            raise SessionError(
                f"active_session.json exists but could not be read ({exc}). "
                "Inspect or remove .debugbrief/active_session.json to recover."
            ) from exc
        if not isinstance(data, dict) or "session_id" not in data:
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
        self._recompute_counts(session)
        atomic_write_json(
            self.paths.session_file(session.session_id), session.to_dict()
        )

    def load_session_file(self, session_id: str) -> Session:
        path = self.paths.session_file(session_id)
        if not path.exists():
            raise SessionError(f"Session file not found for id {session_id}.")
        try:
            return Session.from_dict(read_json(path))
        except (ValueError, OSError) as exc:
            raise SessionError(f"Could not read session {session_id}: {exc}") from exc

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
        if self.has_active():
            existing = self._read_active_pointer() or {}
            raise SessionError(
                "A DebugBrief session is already active"
                + (f" ({existing.get('title')!r})." if existing.get("title") else ".")
                + " End it with: debugbrief end --mode pr|handoff|incident, "
                "or check it with: debugbrief status"
            )

        clean_title = title.strip()
        if not clean_title:
            raise SessionError("Session title must not be empty.")
        # The title can come straight from a command line (auto-start seeds it
        # from the raw command), so scrub secrets before it is persisted to the
        # session file and the active pointer, the same as command output.
        clean_title, _ = redact_text(clean_title)

        self.paths.ensure_directories()
        git_state = git_utils.capture_state(self.paths.project_root, initial=True)
        if git_state.is_repo:
            # Baseline of files already changed before the session, so the final
            # report counts only what the session actually changed.
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

    def record_command(self, result: RunResult) -> Session:
        # Lightweight git snapshot at the moment of the command, taken outside
        # the lock since it only reads the working tree, so reports can later
        # correlate file changes. Safe and silent outside a repo.
        if self.paths.is_git_repo:
            cwd = self.paths.project_root
            result.command_data.git_head = git_utils.current_short_sha(cwd)
            result.command_data.git_changed_files = git_utils.changed_files(cwd)
        with self._repo_lock():
            session = self.require_active("run a command")
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
            self._write_active_pointer(session)
            return session

    def end(self, mode: str, report_format: str = "md", detail: str = "full") -> Session:
        # Local imports avoid an import cycle with the reporters package.
        import json

        from .reporters import render_report, render_report_json
        from .utils import write_text

        with self._repo_lock():
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
        result: Dict[str, Any] = {"action": "none", "detail": "", "corrupt": []}
        if self.has_active():
            try:
                session = self.load_active()
            except SessionError as exc:
                self._clear_active_pointer()
                result["action"] = "cleared_broken_pointer"
                result["detail"] = str(exc)
            else:
                if session is not None and session.status == SessionStatus.ACTIVE.value:
                    result["action"] = "healthy"
                    result["detail"] = session.title
                else:
                    self._clear_active_pointer()
                    result["action"] = "cleared_stale_pointer"
                    result["detail"] = session.status if session else "unknown"

        sessions_dir = self.paths.sessions_dir
        if sessions_dir.is_dir():
            for path in sorted(sessions_dir.glob("*.json")):
                try:
                    Session.from_dict(read_json(path))
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
