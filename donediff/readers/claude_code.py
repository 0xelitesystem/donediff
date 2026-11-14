"""The Claude Code reader: ~/.claude/projects JSONL transcripts and their sub-agents.

The format work lives in donediff.parser and donediff.delegated; this module
is the reader those two make up. Claude Code's tool names map to the shared
actions through donediff.models.CLAUDE_CODE_ACTIONS.
"""

from __future__ import annotations

from pathlib import Path

from .. import delegated, parser
from ..models import Session


class ClaudeCodeReader:
    name = "claude-code"

    def discover(self, project_filter: str | None = None) -> list[Path]:
        return parser.discover_transcripts(project_filter)

    def resolve(self, target: str, project_filter: str | None = None) -> Path:
        return parser.resolve_target(target, project_filter)

    def parse(self, path: Path) -> Session:
        return parser.parse_transcript(path)

    def attach_sources(self, session: Session) -> Session:
        return delegated.attach_sources(session)


CLAUDE_CODE = ClaudeCodeReader()
