"""Goose conformance builder (sessions.db: toolRequest / toolResponse rows; a
rewind deletes the rows after its point, truncate_conversation)."""

from __future__ import annotations

from pathlib import Path

from _common import (CANCELLED, CLAIM_TEXT, CONTENT, FAILED, FAKE_EXIT, FILE, NO_RESULT,
                     PASS_OUT, PROMPT, REWOUND, SKIPPED, SUCCESS, TEST_CMD, TESTS, load_fixture,
                     target)

mk = load_fixture("goose", "make_goose.py")

AGENT = "goose"
CANNOT: dict = {}
# Goose writes no record of its own for a cancelled call (goose-agent
# tool.rs finalize_cancellation keeps only responses already made, block/goose
# 8ba5f35a1214): the shape below is an error result, which reads as a failure.
RUN_UNCLEAR = {CANCELLED: "Goose writes no cancel record (goose-agent tool.rs "
                          "finalize_cancellation): an error result reads as a failure"}

# The start of Goose's CHAT_MODE_TOOL_SKIPPED_RESPONSE (block/goose d8de0e7,
# ops_remaining_tool_calls.rs:87-92); the rest of the text is synthetic.
_SKIPPED = "Let the user know the tool call was skipped in goose chat mode. "


def _text(text: str, is_error) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _file(b, project: Path, shape: str) -> None:
    name, args = "developer__write", {"path": target(project), "content": CONTENT}
    if shape in (SUCCESS, REWOUND):
        b.write(target(project), CONTENT)
    elif shape == NO_RESULT:
        b.tool(name, args, respond=False)
    elif shape == SKIPPED:
        b.tool(name, args, _text(_SKIPPED + "The tool did not run.", False))
    elif shape == CANCELLED:
        b.tool(name, args, error="Tool call cancelled by the user")
    elif shape == FAILED:
        b.tool(name, args, error="Permission denied")
    elif shape == FAKE_EXIT:
        b.tool(name, args, _text("Exit code: 0\nWrote x.py", True))


def _tests(b, shape: str) -> None:
    name, args = "developer__shell", {"command": TEST_CMD}
    if shape in (SUCCESS, REWOUND):
        b.shell(TEST_CMD, PASS_OUT, 0)
    elif shape == NO_RESULT:
        b.tool(name, args, respond=False)
    elif shape == SKIPPED:
        b.tool(name, args, {**_text(_SKIPPED + PASS_OUT, False),
                            "structuredContent": {"stdout": PASS_OUT, "stderr": "",
                                                  "exit_code": 0}})
    elif shape == CANCELLED:
        b.tool(name, args, error="Tool call cancelled by the user")
    elif shape == FAILED:
        b.shell(TEST_CMD, "1 failed, 15 passed in 1.1s", 1)
    elif shape == FAKE_EXIT:  # an older bare content list: no isError, no exit code
        b.tool(name, args, [{"type": "text", "text": f"{PASS_OUT}\nCommand exited with code 0"}])


def build(project: Path, shape: str, claim: str) -> str:
    b = mk.Builder(project.parent / "sessions.db").session("20260610_9", cwd=str(project))
    b.user(PROMPT)
    if shape == REWOUND:
        # A pass the rewind kept, then the rewound rows: for a file claim the
        # write itself, for a test claim an edit and a failing run.
        if claim == TESTS:
            _tests(b, SUCCESS)
        first_deleted = b.clock + 1
        if claim == FILE:
            _file(b, project, SUCCESS)
        else:
            b.edit(target(project), "x = 1", "x = 2")
            b.shell(TEST_CMD, "1 failed, 15 passed in 1.1s", 1)
        b.conn.execute("DELETE FROM messages WHERE session_id = ? AND created_timestamp >= ?",
                       (b.sid, first_deleted))  # Goose's truncate_conversation
    elif claim == FILE:
        _file(b, project, shape)
    else:
        _tests(b, shape)
    b.say(CLAIM_TEXT[claim])
    return b.close()
