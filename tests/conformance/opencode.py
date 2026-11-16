"""opencode conformance builder (SQLite, schema v2: tool parts with state.status,
shell exit code in state.metadata.exit)."""

from __future__ import annotations

from pathlib import Path

from _common import (CANCELLED, CLAIM_TEXT, CONTENT, FAILED, FAKE_EXIT, FILE, NO_RESULT,
                     PASS_OUT, PROMPT, REWOUND, SKIPPED, SUCCESS, TEST_CMD, TESTS,
                     load_fixture, target)

mo = load_fixture("opencode", "make_opencode.py")

AGENT = "opencode"
_OPEN = ("open (D087): opencode.py reads no session revert (undo) record, so the reader "
         "has no rewound call to build; whether a reverted part stays in the store is unconfirmed")
CANNOT = {(REWOUND, FILE): _OPEN, (REWOUND, TESTS): _OPEN}

_REJECTED = "The user rejected permission to use this specific tool call."
_ABORTED = "Tool execution aborted"


def build(project: Path, shape: str, claim: str) -> str:
    b = mo.Builder(project.parent / "opencode.db", "v2").session("ses_conf0001",
                                                                  directory=str(project))
    b.user(PROMPT)
    if claim == FILE:
        name = "write"
        args = {mo.PATH_KEY["v2"]: target(project), "content": CONTENT}
        if shape == SUCCESS:
            b.write(target(project), CONTENT)
        elif shape == NO_RESULT:
            b.tool(name, args, status="running")
        else:
            error = {SKIPPED: _REJECTED, CANCELLED: _ABORTED, FAILED: "File not found.",
                     FAKE_EXIT: "Exit code: 0\nWrote file successfully."}[shape]
            b.tool(name, args, status="error", error=error)
    else:
        shell = mo.SHELL_TOOL["v2"]
        if shape == SUCCESS:
            b.shell(TEST_CMD, PASS_OUT, 0)
        elif shape == NO_RESULT:
            b.shell(TEST_CMD, status="running")
        elif shape == FAILED:
            b.shell(TEST_CMD, "1 failed, 15 passed in 1.1s", 1)
        elif shape == FAKE_EXIT:  # completed, no exit code recorded, the text claims 0
            b.shell(TEST_CMD, f"{PASS_OUT}\nExit code: 0", exit=None)
        else:
            b.tool(shell, {"command": TEST_CMD}, status="error",
                   error=_REJECTED if shape == SKIPPED else _ABORTED)
    b.say(CLAIM_TEXT[claim])
    return b.close()
