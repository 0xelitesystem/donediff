"""Transcript readers, in the order "auto" tries them.

How "auto" picks a reader:
- a target that is a file: the first reader whose detect() is True for it,
  else Claude Code (an empty or unknown file reads as before);
- "latest": Claude Code, unless it has no sessions; then the first of Codex,
  opencode, Gemini CLI, Qwen Code and Cline that has some;
- a session-id prefix: the first of Claude Code, Codex, opencode, Gemini CLI,
  Qwen Code and Cline with a matching session, else Claude Code (whose "not
  found" message is shown);
  an opencode database path with "#<session-id prefix>" goes to opencode;
  Goose (sessions.db) comes after these in every step above, likewise;
- `list`: as "latest".
"""

from __future__ import annotations

from pathlib import Path

from .base import AmbiguousSession, NotATranscript, Reader, UnreadableSession, short_ids
from .claude_code import CLAUDE_CODE
from .cline import CLINE
from .codex import CODEX
from .gemini import GEMINI
from .opencode import OPENCODE
from .qwen import QWEN
from .goose import GOOSE

READERS: tuple[Reader, ...] = (CLAUDE_CODE, CODEX, OPENCODE, GEMINI, QWEN, CLINE, GOOSE)
AGENT_CHOICES = ("auto", *(r.name for r in READERS))

__all__ = ["AGENT_CHOICES", "READERS", "AmbiguousSession", "NotATranscript", "Reader",
           "UnreadableSession", "not_a_transcript", "select", "select_for_list", "short_ids"]


def _named(agent: str) -> Reader:
    for reader in READERS:
        if reader.name == agent:
            return reader
    raise ValueError(f"unknown agent {agent!r}; choose from {', '.join(AGENT_CHOICES)}")


def _by_sessions(project: str | None) -> Reader:
    if CLAUDE_CODE.discover(project):
        return CLAUDE_CODE
    return next((r for r in READERS[1:] if r.discover(project)), CLAUDE_CODE)


def select(agent: str = "auto", target: str | None = None,
           project: str | None = None) -> Reader:
    """The reader for --agent and the audit target (see the module docstring)."""
    if agent != "auto":
        return _named(agent)
    if target is None:
        return READERS[0]
    path = Path(target)
    if path.is_file():
        return next((r for r in READERS if r.detect(path)), READERS[0])
    if target == "latest":
        return _by_sessions(project)
    for reader in READERS:
        try:
            reader.resolve(target, project)
        except FileNotFoundError:
            continue
        return reader
    return READERS[0]


def not_a_transcript(reader: Reader, path: Path, unreadable: int,
                     agent: str = "auto") -> NotATranscript:
    """The plain message for a file this reader found nothing in (QA-027)."""
    try:
        empty = Path(path).stat().st_size == 0
    except OSError:
        empty = False
    if empty:
        why = "the file is empty"
    elif unreadable:
        why = f"{unreadable} line(s) could not be read"
    else:
        why = "no record in it could be read"
    message = f"{path} does not look like a {reader.label} transcript ({why})"
    if agent != "auto":
        other = next((r for r in READERS if r is not reader and r.detect(Path(path))), None)
        message += (f". Try --agent auto, or --agent {other.name}, which reads it"
                    if other else ". Try --agent auto")
    return NotATranscript(message)


def select_for_list(agent: str = "auto", project: str | None = None) -> Reader:
    """The reader whose sessions `donediff list` shows."""
    return _named(agent) if agent != "auto" else _by_sessions(project)
