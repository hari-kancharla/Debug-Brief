"""DebugBrief: a local-first CLI for recording debugging sessions.

DebugBrief captures the useful path of a debugging session -- notes, executed
commands and their outcomes, Git state changes, and verification steps -- and
turns it into a concise, honest markdown brief (PR, handoff, or incident).

It uses the Python standard library and native ``git``, with one conditional
dependency: the ``tomli`` TOML parser on Python < 3.11 (3.11+ uses the
standard-library ``tomllib``). No AI, no telemetry, no cloud sync, no daemon.
"""

__version__ = "1.3.0"

__all__ = ["__version__"]
