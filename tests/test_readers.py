"""The reader layer: registry, --agent, and the shared tool actions."""

from __future__ import annotations

import pytest

from donediff import cli
from donediff.models import (
    CLAUDE_CODE_ACTIONS, EDIT, FILE_ACTIONS, MULTI_EDIT, NOTEBOOK_EDIT, OTHER, SHELL, WRITE,
    Event, EventKind,
)
from donediff.readers import AGENT_CHOICES, READERS, select
from donediff.readers.claude_code import CLAUDE_CODE


def test_registry_has_claude_code_first():
    assert READERS[0] is CLAUDE_CODE
    assert AGENT_CHOICES == ("auto", "claude-code", "codex", "opencode", "gemini", "qwen",
                              "cline")


def test_select():
    assert select("auto") is CLAUDE_CODE
    assert select("claude-code") is CLAUDE_CODE
    with pytest.raises(ValueError):
        select("nope")


def test_agent_flag_parses_and_rejects_unknown():
    args = cli.build_parser().parse_args(["audit", "x.jsonl", "--agent", "claude-code"])
    assert args.agent == "claude-code"
    assert cli.build_parser().parse_args(["list"]).agent == "auto"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["audit", "x.jsonl", "--agent", "nope"])


def test_every_claude_tool_name_the_old_checks_used_maps_to_its_action():
    assert CLAUDE_CODE_ACTIONS == {
        "Bash": SHELL, "PowerShell": SHELL, "Write": WRITE, "Edit": EDIT,
        "MultiEdit": MULTI_EDIT, "NotebookEdit": NOTEBOOK_EDIT,
    }
    assert set(FILE_ACTIONS) == {WRITE, EDIT, MULTI_EDIT, NOTEBOOK_EDIT}


def _call(name: str = "", action: str = "", **tool_input) -> Event:
    return Event(kind=EventKind.TOOL_CALL, index=0, tool_name=name, action=action,
                 tool_input=tool_input)


def test_action_from_name_and_from_reader():
    assert _call("Bash", command="ls").command == "ls"
    assert _call("Agent").tool_action == OTHER
    # a reader for another agent sets the action; its own tool name does not matter
    assert _call("exec_command", SHELL, command="pytest").command == "pytest"
    assert _call("apply_patch", EDIT, file_path="a.py").is_file_edit()
    assert not _call("read_file", OTHER, file_path="a.py").is_file_edit()


def test_run_audit_takes_agent(tmp_path):
    from conftest import assistant_text, write_jsonl
    path = write_jsonl(tmp_path / "s.jsonl", [assistant_text("Hello.")])
    assert cli.run_audit(path, check_disk=False, agent="claude-code").score is None
