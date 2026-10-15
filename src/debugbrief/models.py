"""Typed data models for DebugBrief sessions and events.

All persisted state flows through these dataclasses. Every model knows how to
serialize itself to plain JSON-compatible dicts and reconstruct itself from
them, which keeps persistence honest and round-trippable.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .markdown import normalize_text


class SessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    ABANDONED = "ABANDONED"


class EventType(str, Enum):
    COMMAND = "command"
    NOTE = "note"
    SNAPSHOT = "snapshot"
    WARNING = "warning"


# Status values for a captured command.
COMMAND_STATUS_PASSED = "passed"
COMMAND_STATUS_FAILED = "failed"
COMMAND_STATUS_TIMED_OUT = "timed_out"
COMMAND_STATUS_ERROR = "error"  # could not execute (e.g. command not found)
COMMAND_STATUS_INTERRUPTED = "interrupted"  # stopped by the user (Ctrl-C)
COMMAND_STATUS_BROKEN_PIPE = "broken_pipe"  # downstream consumer closed the pipe

# Statuses that mean a command did not complete successfully. Used wherever the
# code counts failures or decides what to surface as "tried but did not pass".
NON_SUCCESS_STATUSES = (
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_TIMED_OUT,
    COMMAND_STATUS_ERROR,
    COMMAND_STATUS_INTERRUPTED,
    COMMAND_STATUS_BROKEN_PIPE,
)


def _as_dict(value: Any) -> Dict[str, Any]:
    """Return a mapping leaf, or an empty mapping for malformed state."""
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    """Return a finite integer for a persisted numeric leaf."""
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
        finite = math.isfinite(float(result))
    except (TypeError, ValueError, OverflowError):
        return default
    return result if finite else default


def _as_float(value: Any, default: float = 0.0) -> float:
    """Return a finite float for a persisted numeric leaf."""
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _as_bool(value: Any, default: bool = False) -> bool:
    """Return a real boolean leaf, without treating arbitrary strings as true."""
    return value if isinstance(value, bool) else default


def _as_text(value: Any, default: str = "") -> str:
    """Return a text leaf, using the default for malformed persisted values."""
    return value if isinstance(value, str) else default


def _as_optional_text(value: Any) -> Optional[str]:
    """Return a text leaf or ``None`` when it is absent or malformed."""
    return value if isinstance(value, str) else None


def _as_string_list(value: Any) -> List[str]:
    """Return only string members from a persisted list leaf."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _as_list(value: Any) -> List[Any]:
    """Return a list leaf, or an empty list for a malformed value."""
    return value if isinstance(value, list) else []


def _as_stat_map(value: Any) -> Dict[str, Tuple[int, int]]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, Tuple[int, int]] = {}
    for key, stats in value.items():
        if not isinstance(key, str) or not isinstance(stats, (list, tuple)):
            continue
        if len(stats) != 2:
            continue
        added, deleted = (_as_int(stats[0]), _as_int(stats[1]))
        result[key] = (added, deleted)
    return result


@dataclass
class GitState:
    is_repo: bool = False
    repo_root: Optional[str] = None
    initial_sha: Optional[str] = None
    final_sha: Optional[str] = None
    branch: Optional[str] = None
    detached_head: bool = False
    # Fingerprints (path -> content hash) of files already changed when the
    # session started, so the final report can tell which files actually changed
    # during the session. Empty for older sessions and outside a repo.
    initial_dirty: Dict[str, str] = field(default_factory=dict)
    initial_dirty_stats: Dict[str, Tuple[int, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.is_repo = _as_bool(self.is_repo)
        self.repo_root = _as_optional_text(self.repo_root)
        self.initial_sha = _as_optional_text(self.initial_sha)
        self.final_sha = _as_optional_text(self.final_sha)
        self.branch = _as_optional_text(self.branch)
        if self.repo_root is not None:
            self.repo_root = normalize_text(self.repo_root)
        if self.initial_sha is not None:
            self.initial_sha = normalize_text(self.initial_sha)
        if self.final_sha is not None:
            self.final_sha = normalize_text(self.final_sha)
        if self.branch is not None:
            self.branch = normalize_text(self.branch)
        self.detached_head = _as_bool(self.detached_head)
        if not isinstance(self.initial_dirty, dict):
            self.initial_dirty = {}
        else:
            self.initial_dirty = {
                key: value
                for key, value in self.initial_dirty.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        self.initial_dirty_stats = _as_stat_map(self.initial_dirty_stats)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_repo": self.is_repo,
            "repo_root": self.repo_root,
            "initial_sha": self.initial_sha,
            "final_sha": self.final_sha,
            "branch": self.branch,
            "detached_head": self.detached_head,
            "initial_dirty": dict(self.initial_dirty),
            "initial_dirty_stats": {
                path: [added, deleted]
                for path, (added, deleted) in self.initial_dirty_stats.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GitState":
        data = _as_dict(data)
        initial_dirty = data.get("initial_dirty")
        if not isinstance(initial_dirty, dict):
            initial_dirty = {}
        initial_dirty_stats = _as_stat_map(data.get("initial_dirty_stats"))
        return cls(
            is_repo=_as_bool(data.get("is_repo")),
            repo_root=_as_optional_text(data.get("repo_root")),
            initial_sha=_as_optional_text(data.get("initial_sha")),
            final_sha=_as_optional_text(data.get("final_sha")),
            branch=_as_optional_text(data.get("branch")),
            detached_head=_as_bool(data.get("detached_head")),
            initial_dirty={
                key: value
                for key, value in initial_dirty.items()
                if isinstance(key, str) and isinstance(value, str)
            },
            initial_dirty_stats=initial_dirty_stats,
        )


@dataclass
class Timestamps:
    start: Optional[str] = None
    end: Optional[str] = None

    def __post_init__(self) -> None:
        self.start = _as_optional_text(self.start)
        self.end = _as_optional_text(self.end)
        if self.start is not None:
            self.start = normalize_text(self.start)
        if self.end is not None:
            self.end = normalize_text(self.end)

    def to_dict(self) -> Dict[str, Any]:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Timestamps":
        data = _as_dict(data)
        return cls(
            start=_as_optional_text(data.get("start")),
            end=_as_optional_text(data.get("end")),
        )


@dataclass
class CommandClassification:
    is_test: bool = False
    is_verification: bool = False
    tool: Optional[str] = None
    status: str = COMMAND_STATUS_FAILED

    def __post_init__(self) -> None:
        self.is_test = _as_bool(self.is_test)
        self.is_verification = _as_bool(self.is_verification)
        self.tool = _as_optional_text(self.tool)
        self.status = _as_text(self.status, COMMAND_STATUS_FAILED) or COMMAND_STATUS_FAILED
        if self.tool is not None:
            self.tool = normalize_text(self.tool).strip() or None
        self.status = normalize_text(self.status).strip().lower()
        if self.status not in {
            COMMAND_STATUS_PASSED,
            *NON_SUCCESS_STATUSES,
        }:
            self.status = COMMAND_STATUS_FAILED
        if self.status != COMMAND_STATUS_PASSED:
            self.is_verification = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_test": self.is_test,
            "is_verification": self.is_verification,
            "tool": self.tool,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommandClassification":
        data = _as_dict(data)
        return cls(
            is_test=_as_bool(data.get("is_test")),
            is_verification=_as_bool(data.get("is_verification")),
            tool=_as_optional_text(data.get("tool")),
            status=_as_text(data.get("status"), COMMAND_STATUS_FAILED)
            or COMMAND_STATUS_FAILED,
        )


@dataclass
class CommandData:
    """The ``data`` payload stored inside a command event."""

    command: str
    started_at: str
    ended_at: str
    duration_seconds: float
    exit_code: Optional[int]
    stdout_preview: str = ""
    stderr_preview: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    used_shell: bool = False
    classification: CommandClassification = field(default_factory=CommandClassification)
    # Whether redaction masked anything in the stored command/output.
    redacted: bool = False
    # Lightweight git snapshot taken at the moment this command was recorded.
    # Empty/None outside a repo or when git was unavailable (backward compatible:
    # older session files simply omit these).
    git_head: Optional[str] = None
    git_changed_files: List[str] = field(default_factory=list)
    git_fingerprints: Dict[str, str] = field(default_factory=dict)
    # Directory the command actually ran in (the user's cwd). Used to tell apart
    # same-named checks run in different directories. None for older sessions.
    invocation_cwd: Optional[str] = None
    # Unique id for this captured command, so a retried persistence cannot append
    # the same result twice. None for older sessions.
    command_id: Optional[str] = None

    def __post_init__(self) -> None:
        self.command = _as_text(self.command)
        self.started_at = _as_text(self.started_at)
        self.ended_at = _as_text(self.ended_at)
        self.duration_seconds = _as_float(self.duration_seconds, 0.0)
        if not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool):
            self.exit_code = None
        self.stdout_preview = _as_text(self.stdout_preview)
        self.stderr_preview = _as_text(self.stderr_preview)
        self.stdout_truncated = _as_bool(self.stdout_truncated)
        self.stderr_truncated = _as_bool(self.stderr_truncated)
        self.used_shell = _as_bool(self.used_shell)
        if not isinstance(self.classification, CommandClassification):
            self.classification = CommandClassification.from_dict(
                self.classification if isinstance(self.classification, dict) else {}
            )
        if self.classification.status == COMMAND_STATUS_PASSED and self.exit_code != 0:
            self.classification.status = COMMAND_STATUS_FAILED
        if self.classification.status != COMMAND_STATUS_PASSED:
            self.classification.is_verification = False
        self.redacted = _as_bool(self.redacted)
        self.git_head = _as_optional_text(self.git_head)
        self.git_changed_files = _as_string_list(self.git_changed_files)
        self.git_fingerprints = (
            {
                key: value
                for key, value in self.git_fingerprints.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            if isinstance(self.git_fingerprints, dict)
            else {}
        )
        self.invocation_cwd = _as_optional_text(self.invocation_cwd)
        self.command_id = _as_optional_text(self.command_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
            "stdout_preview": self.stdout_preview,
            "stderr_preview": self.stderr_preview,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "used_shell": self.used_shell,
            "classification": self.classification.to_dict(),
            "redacted": self.redacted,
            "git_head": self.git_head,
            "git_changed_files": list(self.git_changed_files),
            "git_fingerprints": dict(self.git_fingerprints),
            "invocation_cwd": self.invocation_cwd,
            "command_id": self.command_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommandData":
        data = _as_dict(data)
        exit_code = data.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            exit_code = None
        return cls(
            command=_as_text(data.get("command")),
            started_at=_as_text(data.get("started_at")),
            ended_at=_as_text(data.get("ended_at")),
            duration_seconds=_as_float(data.get("duration_seconds"), 0.0),
            exit_code=exit_code,
            stdout_preview=_as_text(data.get("stdout_preview")),
            stderr_preview=_as_text(data.get("stderr_preview")),
            stdout_truncated=_as_bool(data.get("stdout_truncated")),
            stderr_truncated=_as_bool(data.get("stderr_truncated")),
            used_shell=_as_bool(data.get("used_shell")),
            classification=CommandClassification.from_dict(
                data.get("classification", {})
            ),
            redacted=_as_bool(data.get("redacted")),
            git_head=_as_optional_text(data.get("git_head")),
            git_changed_files=_as_string_list(data.get("git_changed_files")),
            git_fingerprints=(
                {
                    key: value
                    for key, value in data.get("git_fingerprints", {}).items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                if isinstance(data.get("git_fingerprints", {}), dict)
                else {}
            ),
            invocation_cwd=_as_optional_text(data.get("invocation_cwd")),
            command_id=_as_optional_text(data.get("command_id")),
        )


@dataclass
class Event:
    type: str
    timestamp: str
    data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.type = _as_text(self.type)
        self.timestamp = _as_text(self.timestamp)
        self.data = _as_dict(self.data)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "timestamp": self.timestamp, "data": self.data}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        data = _as_dict(data)
        return cls(
            type=_as_text(data.get("type")),
            timestamp=_as_text(data.get("timestamp")),
            data=_as_dict(data.get("data")),
        )

    # Convenience constructors -------------------------------------------------
    @classmethod
    def note(cls, text: str, timestamp: str) -> "Event":
        return cls(type=EventType.NOTE.value, timestamp=timestamp, data={"text": text})

    @classmethod
    def warning(cls, message: str, timestamp: str) -> "Event":
        return cls(
            type=EventType.WARNING.value,
            timestamp=timestamp,
            data={"message": message},
        )

    @classmethod
    def command(cls, command_data: CommandData, timestamp: str) -> "Event":
        return cls(
            type=EventType.COMMAND.value,
            timestamp=timestamp,
            data=command_data.to_dict(),
        )

    @classmethod
    def snapshot(cls, payload: Dict[str, Any], timestamp: str) -> "Event":
        return cls(type=EventType.SNAPSHOT.value, timestamp=timestamp, data=payload)


def valid_timestamp(value: Any) -> Optional[str]:
    """Return a normalized ISO timestamp, or ``None`` for an invalid leaf."""
    text = _as_optional_text(value)
    if text is None or not text.strip():
        return None
    from .utils import parse_iso8601

    try:
        parse_iso8601(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return text.strip()


def effective_event_timestamp(event: "Event") -> str:
    """Return the first valid timestamp representing when an event occurred."""
    event_type = getattr(event, "type", "")
    event_timestamp = getattr(event, "timestamp", "")
    data = _as_dict(getattr(event, "data", {}))
    candidates: Tuple[Any, ...]
    if event_type == EventType.COMMAND.value:
        candidates = (data.get("ended_at"), event_timestamp, data.get("started_at"))
    else:
        candidates = (event_timestamp,)
    for candidate in candidates:
        timestamp = valid_timestamp(candidate)
        if timestamp is not None:
            return timestamp
    return ""


@dataclass
class FileChange:
    """A single changed file with a name-status label (M/A/D/R)."""

    status: str
    path: str

    def __post_init__(self) -> None:
        self.status = _as_text(self.status, "M") or "M"
        self.path = _as_text(self.path)

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "path": self.path}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileChange":
        data = _as_dict(data)
        return cls(
            status=_as_text(data.get("status"), "M") or "M",
            path=_as_text(data.get("path")),
        )


@dataclass
class Summary:
    modified_files: List[str] = field(default_factory=list)
    file_changes: List[FileChange] = field(default_factory=list)
    lines_added: int = 0
    lines_deleted: int = 0
    tests_run: List[str] = field(default_factory=list)
    notes_count: int = 0
    commands_count: int = 0
    failed_commands_count: int = 0
    command_capture_status: str = "full"

    def __post_init__(self) -> None:
        self.modified_files = _as_string_list(self.modified_files)
        if not isinstance(self.file_changes, list):
            self.file_changes = []
        else:
            self.file_changes = [
                change if isinstance(change, FileChange) else FileChange.from_dict(change)
                for change in self.file_changes
            ]
        self.lines_added = _as_int(self.lines_added)
        self.lines_deleted = _as_int(self.lines_deleted)
        self.tests_run = _as_string_list(self.tests_run)
        self.notes_count = _as_int(self.notes_count)
        self.commands_count = _as_int(self.commands_count)
        self.failed_commands_count = _as_int(self.failed_commands_count)
        self.command_capture_status = _as_text(
            self.command_capture_status, "full"
        ) or "full"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "modified_files": list(self.modified_files),
            "file_changes": [fc.to_dict() for fc in self.file_changes],
            "lines_added": self.lines_added,
            "lines_deleted": self.lines_deleted,
            "tests_run": list(self.tests_run),
            "notes_count": self.notes_count,
            "commands_count": self.commands_count,
            "failed_commands_count": self.failed_commands_count,
            "command_capture_status": self.command_capture_status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Summary":
        data = _as_dict(data)
        file_changes = data.get("file_changes")
        if not isinstance(file_changes, list):
            file_changes = []
        return cls(
            modified_files=_as_string_list(data.get("modified_files")),
            file_changes=[FileChange.from_dict(fc) for fc in file_changes],
            lines_added=_as_int(data.get("lines_added")),
            lines_deleted=_as_int(data.get("lines_deleted")),
            tests_run=_as_string_list(data.get("tests_run")),
            notes_count=_as_int(data.get("notes_count")),
            commands_count=_as_int(data.get("commands_count")),
            failed_commands_count=_as_int(data.get("failed_commands_count")),
            command_capture_status=_as_text(
                data.get("command_capture_status"), "full"
            )
            or "full",
        )


@dataclass
class Session:
    title: str
    project_root: str
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = SessionStatus.ACTIVE.value
    warnings: List[str] = field(default_factory=list)
    git: GitState = field(default_factory=GitState)
    timestamps: Timestamps = field(default_factory=Timestamps)
    events: List[Event] = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)

    def __post_init__(self) -> None:
        self.title = normalize_text(_as_text(self.title))
        self.project_root = normalize_text(_as_text(self.project_root))
        self.session_id = _as_text(self.session_id)
        self.status = normalize_text(
            _as_text(self.status, SessionStatus.ACTIVE.value)
        )
        self.warnings = (
            [normalize_text(warning) for warning in self.warnings if isinstance(warning, str)]
            if isinstance(self.warnings, list)
            else []
        )
        if not isinstance(self.git, GitState):
            self.git = GitState.from_dict(
                self.git if isinstance(self.git, dict) else {}
            )
        if not isinstance(self.timestamps, Timestamps):
            self.timestamps = Timestamps.from_dict(
                self.timestamps if isinstance(self.timestamps, dict) else {}
            )
        if not isinstance(self.summary, Summary):
            self.summary = Summary.from_dict(
                self.summary if isinstance(self.summary, dict) else {}
            )
        if not isinstance(self.events, list):
            self.events = []
        else:
            self.events = [
                event if isinstance(event, Event) else Event.from_dict(event)
                for event in self.events
            ]

    # Accessors ---------------------------------------------------------------
    def command_events(self) -> List[Event]:
        if not isinstance(self.events, list):
            return []
        return [
            event
            for event in self.events
            if isinstance(event, Event) and event.type == EventType.COMMAND.value
        ]

    def note_events(self) -> List[Event]:
        if not isinstance(self.events, list):
            return []
        return [
            event
            for event in self.events
            if isinstance(event, Event) and event.type == EventType.NOTE.value
        ]

    def add_warning(self, message: str, timestamp: str) -> None:
        """Record a warning in the warnings list and the event timeline.

        The text is redacted first: a warning can echo a command, an error
        message, or a path, so a secret must never reach disk through it. This is
        the single choke point, so every persisted warning is scrubbed.
        """
        from .markdown import normalize_text
        from .redaction import redact_text

        message = normalize_text(message)
        message, redaction_count = redact_text(message)
        message = normalize_text(message).strip()
        if not message:
            return
        if message not in self.warnings:
            self.warnings.append(message)
        event = Event.warning(message, timestamp)
        if redaction_count:
            event.data["redacted"] = True
        self.events.append(event)

    # Serialization -----------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        git = self.git if isinstance(self.git, GitState) else GitState()
        timestamps = (
            self.timestamps if isinstance(self.timestamps, Timestamps) else Timestamps()
        )
        summary = self.summary if isinstance(self.summary, Summary) else Summary()
        events = self.events if isinstance(self.events, list) else []
        warnings = self.warnings if isinstance(self.warnings, list) else []
        return {
            "session_id": _as_text(self.session_id),
            "title": _as_text(self.title),
            "status": _as_text(self.status, SessionStatus.ACTIVE.value),
            "project_root": _as_text(self.project_root),
            "warnings": [warning for warning in warnings if isinstance(warning, str)],
            "git": git.to_dict(),
            "timestamps": timestamps.to_dict(),
            "events": [
                event.to_dict() for event in events if isinstance(event, Event)
            ],
            "summary": summary.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        # A session file must be a JSON object. Reject anything else (a list,
        # string, number, or null from a corrupt or hostile file) with a
        # ValueError the loaders turn into a controlled SessionError, rather than
        # an AttributeError traceback from calling .get on a non-dict.
        if not isinstance(data, dict):
            raise ValueError("session data must be a JSON object")
        # warnings and events carry the timeline; a non-list here is structural
        # corruption (a string would silently split into characters), so reject it
        # rather than load garbage. Leaf values degrade elsewhere; structure does
        # not.
        warnings = data.get("warnings", [])
        if not isinstance(warnings, list):
            raise ValueError("session 'warnings' must be a list")
        events = data.get("events", [])
        if not isinstance(events, list):
            raise ValueError("session 'events' must be a list")
        return cls(
            session_id=_as_text(data.get("session_id"), str(uuid.uuid4()))
            or str(uuid.uuid4()),
            title=_as_text(data.get("title")),
            status=_as_text(data.get("status"), SessionStatus.ACTIVE.value)
            or SessionStatus.ACTIVE.value,
            project_root=_as_text(data.get("project_root")),
            warnings=_as_string_list(warnings),
            git=GitState.from_dict(data.get("git", {})),
            timestamps=Timestamps.from_dict(data.get("timestamps", {})),
            events=[Event.from_dict(e) for e in events],
            summary=Summary.from_dict(data.get("summary", {})),
        )
