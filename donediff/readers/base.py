"""What a transcript reader provides. One reader per coding agent.

A reader turns that agent's local session log into a Session whose tool-call
Events carry a shared action and shared input keys (see donediff.models), so
claims, checks, gaming signals and the score never see the agent's format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import Session


class UnreadableSession(OSError):
    """The session file exists but this Python cannot read it (the message says why)."""


class Reader(Protocol):
    name: str  # the --agent value, e.g. "claude-code"
    location: str  # where its sessions live, as messages name it

    def discover(self, project_filter: str | None = None) -> list[Path]:
        """This agent's session files, newest first."""

    def resolve(self, target: str, project_filter: str | None = None) -> Path:
        """A CLI target (path, session-id prefix or 'latest') to a file; FileNotFoundError if none."""

    def detect(self, path: Path) -> bool:
        """True when the file is in this agent's format (read from its first record)."""

    def describe(self, path: Path) -> tuple[str, str]:
        """(short session id, project) for one line of `donediff list`."""

    def parse(self, path: Path) -> Session:
        """The session, with every tool call's action and inputs in the shared shape."""

    def attach_sources(self, session: Session) -> Session:
        """Add delegated transcripts (sub-agents) as evidence; a no-op where the agent has none."""
