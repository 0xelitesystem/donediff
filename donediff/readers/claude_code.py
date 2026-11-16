"""The Claude Code reader: ~/.claude/projects JSONL transcripts and their sub-agents.

The format work lives in donediff.parser and donediff.delegated; this module
is the reader those two make up. Claude Code's tool names map to the shared
actions through donediff.models.CLAUDE_CODE_ACTIONS.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import delegated, parser
from ..models import Session
from .qwen import is_qwen_record

# Record types a Claude Code transcript starts with or holds.
_RECORD_TYPES = frozenset({
    "user", "assistant", "system", "summary", "attachment", "progress",
    "file-history-snapshot", "queue-operation", "custom-title", "tag",
})
_DETECT_LIMIT = 1024 * 1024  # bytes of the first line read to tell the format


def first_record(path: Path, limit: int = _DETECT_LIMIT) -> dict | None:
    """The first JSON object in a file, read from at most `limit` bytes of its first lines."""
    try:
        with open(path, "rb") as fh:
            for _ in range(5):
                line = fh.readline(limit + 1)
                if not line:
                    return None
                if len(line) > limit:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    return None
                return record if isinstance(record, dict) else None
    except OSError:
        return None
    return None


class ClaudeCodeReader:
    name = "claude-code"
    location = "~/.claude/projects"

    def discover(self, project_filter: str | None = None) -> list[Path]:
        return parser.discover_transcripts(project_filter)

    def resolve(self, target: str, project_filter: str | None = None) -> Path:
        return parser.resolve_target(target, project_filter)

    def detect(self, path: Path) -> bool:
        record = first_record(path)
        if record is None or "payload" in record:
            return False
        if "projectHash" in record and "type" not in record:
            return False  # a Gemini CLI session's metadata line, not Claude Code
        if is_qwen_record(record):
            return False  # a Qwen Code record (message parts, not content)
        return record.get("type") in _RECORD_TYPES or "sessionId" in record

    def describe(self, path: Path) -> tuple[str, str]:
        return path.stem[:8], path.parent.name

    def parse(self, path: Path) -> Session:
        return parser.parse_transcript(path)

    def attach_sources(self, session: Session) -> Session:
        return delegated.attach_sources(session)


CLAUDE_CODE = ClaudeCodeReader()
