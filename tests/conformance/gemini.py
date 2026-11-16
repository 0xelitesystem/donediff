"""Gemini CLI conformance builder (chat JSONL: toolCalls with status and result,
$rewindTo for a rewind)."""

from __future__ import annotations

from pathlib import Path

from _common import (CANCELLED, CLAIM_TEXT, CONTENT, FAILED, FAKE_EXIT, FILE, NO_RESULT,
                     PASS_OUT, PROMPT, REWOUND, SKIPPED, SUCCESS, TEST_CMD, TESTS,
                     load_fixture, target)

mg = load_fixture("gemini", "make_gemini.py")

AGENT = "gemini"
CANNOT = {(SUCCESS, TESTS): "Gemini writes \"Exit Code\" only when it is not 0 (shell.ts), so "
                            "a passing run is a status only and never VERIFIED (by design)"}


def _call(clock, project: Path, shape: str, claim: str) -> dict:
    if claim == FILE:
        name, args = "write_file", {"file_path": target(project), "content": CONTENT}
        ok = mg.output("c1", name, f"Successfully created and wrote to new file: {target(project)}.")
        fake = mg.output("c1", name, "Exit Code: 0\nSuccessfully created and wrote to new file.")
        failed = mg.error("c1", name, "File not found.")
    else:
        name, args = "run_shell_command", {"command": TEST_CMD}
        ok = mg.output("c1", name, mg.shell_text(PASS_OUT))
        fake = mg.output("c1", name, mg.shell_text(f"{PASS_OUT}\nExit Code: 0"))
        failed = mg.output("c1", name, mg.shell_text("1 failed, 15 passed in 1.1s", 1))
    status, result = {
        NO_RESULT: ("awaiting_approval", None),
        SKIPPED: ("cancelled", mg.error("c1", name, "The user declined this tool call.")),
        CANCELLED: ("cancelled", mg.output("c1", name, mg.shell_text(PASS_OUT))
                    if claim == TESTS else mg.error("c1", name, "Request cancelled.")),
        FAILED: ("error" if claim == FILE else "success", failed),
        FAKE_EXIT: ("error" if claim == FILE else "success", fake),
        REWOUND: ("success", ok),
        SUCCESS: ("success", ok),
    }[shape]
    return mg.call(clock, "c1", name, args, status, result)


def build(project: Path, shape: str, claim: str) -> str:
    clock = mg.Clock()
    records = [mg.meta(), mg.user(clock, "u1", PROMPT), mg.gemini(clock, "g1", "On it."),
               mg.gemini(clock, "g2", "", [_call(clock, project, shape, claim)])]
    if shape == REWOUND:
        records.append({"$rewindTo": "g2"})  # drops g2 and everything after it
    records.append(mg.gemini(clock, "g3", CLAIM_TEXT[claim]))
    return str(mg.place(project.parent / "gemini-home", records, project=str(project)))
