"""What a transcript reader provides. One reader per coding agent.

A reader turns that agent's local session log into a Session whose tool-call
Events carry a shared action and shared input keys (see donediff.models), so
claims, checks, gaming signals and the score never see the agent's format.
"""

from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path
from typing import Protocol

from ..models import Session
from ..shell import is_remote_path


def _is_link(path: Path) -> bool:
    """A symlink or a Windows junction / reparse point (never followed)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat_mod.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _absolute(value: str | None) -> Path | None:
    """The value as a Path when it is absolute, else None (SEC-216 class).

    A relative or drive-relative data-folder variable is ignored, so a reader
    falls back as if it were unset instead of resolving it against the working
    folder. A share path counts as absolute (older Pythons say "//srv/share" is
    not), so the caller refuses it instead of silently falling back.
    """
    return Path(value) if value and (os.path.isabs(value) or is_remote_path(value)) else None


def _str(value) -> str:
    """The value when it is a string, else "" (a log field of the wrong type)."""
    return value if isinstance(value, str) else ""


def _int(value) -> int | None:
    """The value when it is an int and not a bool, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
