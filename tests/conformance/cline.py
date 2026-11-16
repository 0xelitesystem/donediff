"""Cline conformance builder (<id>.messages.json: tool_use / tool_result blocks,
ToolOperationResult entries, the manifest beside it for the cwd)."""

from __future__ import annotations

from pathlib import Path

from _common import (CANCELLED, CLAIM_TEXT, CONTENT, FAILED, FAKE_EXIT, FILE, NO_RESULT,
                     PASS_OUT, PROMPT, REWOUND, SKIPPED, SUCCESS, TEST_CMD, TESTS,
                     load_fixture, target)

mc = load_fixture("cline", "make_cline.py")

AGENT = "cline"
_OPEN = ("open (D087): cline.py reads one messages.json snapshot and has no rewind "
         "(checkpoint restore) record to read, so no rewound call can be built")
CANNOT = {
    (REWOUND, FILE): _OPEN,
    (REWOUND, TESTS): _OPEN,
    (SUCCESS, TESTS): "Cline writes no exit code for a passing command (executors/bash.ts), "
                      "so a pass is a status only and never VERIFIED (by design)",
}

_DENIED = "The user denied this operation."
_CANCELLED = "Tool execution was cancelled"


def build(project: Path, shape: str, claim: str) -> str:
    doc = mc.Doc(session_id="1781000000000_conf1", cwd=str(project))
    doc.user(PROMPT)
    if claim == FILE:
        name, tool_input = "editor", {"path": target(project), "new_text": CONTENT}
        content = {SUCCESS: mc.op(f"create:{target(project)}", f"Created {target(project)}"),
                   FAILED: mc.op(f"create:{target(project)}", "File not found.", success=False),
                   FAKE_EXIT: "Exit code: 0\nFile written."}
    else:
        name, tool_input = "run_commands", {"commands": [TEST_CMD]}
        content = {SUCCESS: [mc.command_op(TEST_CMD, 0, PASS_OUT)],
                   FAILED: [mc.command_op(TEST_CMD, 1, "1 failed, 15 passed in 1.1s")],
                   FAKE_EXIT: f"{PASS_OUT}\n[Command exited with code 0]"}
    doc.assistant(calls=[mc.call("toolu_1", name, tool_input)])
    if shape in (SKIPPED, CANCELLED):
        text = _DENIED if shape == SKIPPED else _CANCELLED
        doc.results(mc.result("toolu_1", name, text, is_error=True))
    elif shape != NO_RESULT:
        doc.results(mc.result("toolu_1", name, content[shape]))
    doc.assistant(CLAIM_TEXT[claim])
    return str(mc.place(project.parent / "cline-sessions", doc.document(), cwd=str(project)))
