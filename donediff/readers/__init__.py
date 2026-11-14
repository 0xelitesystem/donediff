"""Transcript readers, in the order "auto" tries them. Claude Code is the only one today."""

from __future__ import annotations

from .base import Reader
from .claude_code import CLAUDE_CODE

READERS: tuple[Reader, ...] = (CLAUDE_CODE,)
AGENT_CHOICES = ("auto", *(r.name for r in READERS))


def select(agent: str = "auto") -> Reader:
    """The reader for --agent. "auto" is the first reader until a second one exists."""
    if agent == "auto":
        return READERS[0]
    for reader in READERS:
        if reader.name == agent:
            return reader
    raise ValueError(f"unknown agent {agent!r}; choose from {', '.join(AGENT_CHOICES)}")
