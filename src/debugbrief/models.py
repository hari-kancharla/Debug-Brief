"""Typed data models for DebugBrief sessions and events.

All persisted state flows through these dataclasses. Every model knows how to
serialize itself to plain JSON-compatible dicts and reconstruct itself from
them, which keeps persistence honest and round-trippable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


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


@dataclass
class GitState:
    is_repo: bool = False
    repo_root: Optional[str] = None
    initial_sha: Optional[str] = None
    final_sha: Optional[str] = None
    branch: Optional[str] = None
    detached_head: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_repo": self.is_repo,
            "repo_root": self.repo_root,
            "initial_sha": self.initial_sha,
            "final_sha": self.final_sha,
            "branch": self.branch,
            "detached_head": self.detached_head,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GitState":
        return cls(
            is_repo=bool(data.get("is_repo", False)),
            repo_root=data.get("repo_root"),
            initial_sha=data.get("initial_sha"),
            final_sha=data.get("final_sha"),
            branch=data.get("branch"),
            detached_head=bool(data.get("detached_head", False)),
        )


@dataclass
class Timestamps:
    start: Optional[str] = None
    end: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Timestamps":
        return cls(start=data.get("start"), end=data.get("end"))


@dataclass
class CommandClassification:
    is_test: bool = False
    is_verification: bool = False
    tool: Optional[str] = None
    status: str = COMMAND_STATUS_FAILED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_test": self.is_test,
            "is_verification": self.is_verification,
            "tool": self.tool,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommandClassification":
        return cls(
            is_test=bool(data.get("is_test", False)),
            is_verification=bool(data.get("is_verification", False)),
            tool=data.get("tool"),
            status=data.get("status", COMMAND_STATUS_FAILED),
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
    # Directory the command actually ran in (the user's cwd). Used to tell apart
    # same-named checks run in different directories. None for older sessions.
    invocation_cwd: Optional[str] = None

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
            "invocation_cwd": self.invocation_cwd,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommandData":
        return cls(
            command=data.get("command", ""),
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at", ""),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            exit_code=data.get("exit_code"),
            stdout_preview=data.get("stdout_preview", ""),
            stderr_preview=data.get("stderr_preview", ""),
            stdout_truncated=bool(data.get("stdout_truncated", False)),
            stderr_truncated=bool(data.get("stderr_truncated", False)),
            used_shell=bool(data.get("used_shell", False)),
            classification=CommandClassification.from_dict(
                data.get("classification", {})
            ),
            redacted=bool(data.get("redacted", False)),
            git_head=data.get("git_head"),
            git_changed_files=list(data.get("git_changed_files", [])),
            invocation_cwd=data.get("invocation_cwd"),
        )


@dataclass
class Event:
    type: str
    timestamp: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "timestamp": self.timestamp, "data": self.data}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        return cls(
            type=data.get("type", ""),
            timestamp=data.get("timestamp", ""),
            data=data.get("data", {}) or {},
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


@dataclass
class FileChange:
    """A single changed file with a name-status label (M/A/D/R)."""

    status: str
    path: str

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "path": self.path}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileChange":
        return cls(status=data.get("status", "M"), path=data.get("path", ""))


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
        return cls(
            modified_files=list(data.get("modified_files", [])),
            file_changes=[
                FileChange.from_dict(fc) for fc in data.get("file_changes", [])
            ],
            lines_added=int(data.get("lines_added", 0)),
            lines_deleted=int(data.get("lines_deleted", 0)),
            tests_run=list(data.get("tests_run", [])),
            notes_count=int(data.get("notes_count", 0)),
            commands_count=int(data.get("commands_count", 0)),
            failed_commands_count=int(data.get("failed_commands_count", 0)),
            command_capture_status=data.get("command_capture_status", "full"),
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

    # Accessors ---------------------------------------------------------------
    def command_events(self) -> List[Event]:
        return [e for e in self.events if e.type == EventType.COMMAND.value]

    def note_events(self) -> List[Event]:
        return [e for e in self.events if e.type == EventType.NOTE.value]

    def add_warning(self, message: str, timestamp: str) -> None:
        """Record a warning both in the warnings list and the event timeline."""
        if message not in self.warnings:
            self.warnings.append(message)
        self.events.append(Event.warning(message, timestamp))

    # Serialization -----------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "status": self.status,
            "project_root": self.project_root,
            "warnings": list(self.warnings),
            "git": self.git.to_dict(),
            "timestamps": self.timestamps.to_dict(),
            "events": [e.to_dict() for e in self.events],
            "summary": self.summary.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        return cls(
            session_id=data.get("session_id", str(uuid.uuid4())),
            title=data.get("title", ""),
            status=data.get("status", SessionStatus.ACTIVE.value),
            project_root=data.get("project_root", ""),
            warnings=list(data.get("warnings", [])),
            git=GitState.from_dict(data.get("git", {})),
            timestamps=Timestamps.from_dict(data.get("timestamps", {})),
            events=[Event.from_dict(e) for e in data.get("events", [])],
            summary=Summary.from_dict(data.get("summary", {})),
        )
