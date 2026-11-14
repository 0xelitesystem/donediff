"""What a transcript reader provides. One reader per coding agent.

A reader turns that agent's local session log into a Session whose tool-call
Events carry a shared action and shared input keys (see donediff.models), so
claims, checks, gaming signals and the score never see the agent's format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import Session


class Reader(Protocol):
    name: str  # the --agent value, e.g. "claude-code"

    def discover(self, project_filter: str | None = None) -> list[Path]:
        """This agent's session files, newest first."""

    def resolve(self, target: str, project_filter: str | None = None) -> Path:
        """A CLI target (path, session-id prefix or 'latest') to a file; FileNotFoundError if none."""

    def parse(self, path: Path) -> Session:
        """The session, with every tool call's action and inputs in the shared shape."""

    def attach_sources(self, session: Session) -> Session:
        """Add delegated transcripts (sub-agents) as evidence; a no-op where the agent has none."""
