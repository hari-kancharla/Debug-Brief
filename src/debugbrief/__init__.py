"""DebugBrief: a local-first CLI for recording debugging sessions.

DebugBrief captures the useful path of a debugging session -- notes, executed
commands and their outcomes, Git state changes, and verification steps -- and
turns it into a concise, honest markdown brief (PR, handoff, or incident).

It uses only the Python standard library and native ``git``. No AI, no
telemetry, no cloud sync, no background daemon.
"""

__version__ = "1.2.0"

__all__ = ["__version__"]
