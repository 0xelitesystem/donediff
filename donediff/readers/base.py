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


class NotATranscript(ValueError):
    """The file holds no record the reader could read (the message says what to try)."""


class AmbiguousSession(LookupError):
    """An id prefix matches more than one session (the message lists them)."""


_SHORTEST_ID = 8  # `list` shows at least this much of an id, more where it must
_LISTED = 10  # ids named in an "ambiguous" message, at most


def _matches(pairs: list, target: str) -> list:
    """The candidates an id matches: exact ids first, else every prefix match."""
    exact = [c for c, keys in pairs if target in keys]
    if exact:
        return exact
    return [c for c, keys in pairs if any(k.startswith(target) for k in keys)]


def pick_session(candidates: list, target: str, keys, location: str):
    """The session a CLI target names, for every reader (QA-024).

    `candidates` are the reader's sessions, newest first; `keys(candidate)`
    gives the ids a target may name (the session id first, then any longer
    form, such as the file name). "latest" is the newest. An exact id wins;
    else a prefix of one session's id resolves. A prefix that matches several
    sessions is refused (AmbiguousSession) instead of picking one, so a score
    is never shown for a session the user did not mean.
    """
    if target == "latest":
        if not candidates:
            raise FileNotFoundError(f"no transcripts found under {location}")
        return candidates[0]
    pairs = [(c, tuple(keys(c))) for c in candidates]
    found = _matches(pairs, target)
    if not found:
        raise FileNotFoundError(f'no transcript matching "{target}"')
    if len(found) == 1:
        return found[0]
    names = [_short_id(pairs, c) for c in found[:_LISTED]]
    more = ", ..." if len(found) > _LISTED else ""
    raise AmbiguousSession(f"{target} matches {len(found)} sessions: "
                           f"{', '.join(names)}{more}")


def _short_id(pairs: list, candidate, width: int = _SHORTEST_ID) -> str:
    """The shortest id (at least `width` characters) that names only this candidate."""
    keys = next(k for c, k in pairs if c is candidate)
    for key in keys:
        for size in range(min(width, len(key)), len(key) + 1):
            text = key[:size]
            if text == "latest":
                continue
            found = _matches(pairs, text)
            if len(found) == 1 and found[0] is candidate:
                return text
    return keys[-1] if keys else "?"


def short_ids(candidates: list, shown: list, keys, width: int = _SHORTEST_ID) -> list[str]:
    """For `list`: an id per shown session, each long enough to name only it."""
    pairs = [(c, tuple(keys(c))) for c in candidates]
    known = {id(c) for c, _ in pairs}
    pairs += [(c, tuple(keys(c))) for c in shown if id(c) not in known]
    return [_short_id(pairs, c, width) for c in shown]


class Reader(Protocol):
    name: str  # the --agent value, e.g. "claude-code"
    label: str  # the agent as messages name it, e.g. "Claude Code"
    location: str  # where its sessions live, as messages name it

    def discover(self, project_filter: str | None = None) -> list[Path]:
        """This agent's session files, newest first."""

    def resolve(self, target: str, project_filter: str | None = None) -> Path:
        """A CLI target (path, session-id prefix or 'latest') to a file; FileNotFoundError if none."""

    def detect(self, path: Path) -> bool:
        """True when the file is in this agent's format (read from its first record)."""

    def describe(self, path: Path) -> tuple[str, str]:
        """(short session id, project) for one line of `donediff list`."""

    def session_keys(self, path: Path) -> tuple[str, ...]:
        """The ids a target may name this session by, the session id first (pick_session)."""

    def parse(self, path: Path) -> Session:
        """The session, with every tool call's action and inputs in the shared shape."""

    def attach_sources(self, session: Session) -> Session:
        """Add delegated transcripts (sub-agents) as evidence; a no-op where the agent has none."""
