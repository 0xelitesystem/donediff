"""Codex CLI conformance builder (rollout JSONL, legacy history: shell with JSON
metadata.exit_code, apply_patch as the freeform tool, patch_apply_end)."""

from __future__ import annotations

import json
from pathlib import Path

from _common import (CANCELLED, CLAIM_TEXT, CONTENT, FAILED, FAKE_EXIT, FILE, NO_RESULT,
                     PASS_OUT, PROMPT, REWOUND, SKIPPED, SUCCESS, TEST_CMD, TESTS,
                     load_fixture, target)

mc = load_fixture("codex", "make_codex.py")

AGENT = "codex"
_OPEN = ("open (D087): codex.py reads no rollback or undo record, so the reader has "
         "no rewound call to build; whether the rollout format holds one is unconfirmed")
CANNOT = {(REWOUND, FILE): _OPEN, (REWOUND, TESTS): _OPEN}


def _shell_output(text: str, code: int | None) -> str:
    meta = {"duration_seconds": 1.0} if code is None else {"exit_code": code,
                                                            "duration_seconds": 1.0}
    return json.dumps({"output": text, "metadata": meta})


def build(project: Path, shape: str, claim: str) -> str:
    clock = mc.Clock()
    records = [clock.line("session_meta", {**mc.meta("conformance", "legacy"),
                                           "cwd": str(project)}),
               clock.line("response_item", mc.message("user", PROMPT))]
    if claim == FILE:
        patch = f"*** Begin Patch\n*** Add File: {target(project)}\n+{CONTENT}*** End Patch\n"
        records.append(clock.line("response_item", {
            "type": "custom_tool_call", "status": "completed", "call_id": "p1",
            "name": "apply_patch", "input": patch}))
        output = {SKIPPED: "patch rejected by user", CANCELLED: "aborted",
                  FAILED: "apply_patch verification failed: no such file",
                  FAKE_EXIT: "error: the patch did not apply\nProcess exited with code 0",
                  SUCCESS: f"Success. Updated the following files:\nA {target(project)}\n"}
        end = {SKIPPED: {"success": False, "status": "declined"},
               FAILED: {"success": False}, SUCCESS: {"success": True, "status": "completed"}}
    else:
        records.append(clock.line("response_item", {
            "type": "function_call", "name": "shell", "call_id": "p1",
            "arguments": json.dumps({"command": ["bash", "-lc", TEST_CMD],
                                     "workdir": str(project)})}))
        output = {SKIPPED: "exec command rejected by user", CANCELLED: "aborted",
                  FAILED: _shell_output("1 failed, 15 passed in 1.1s", 1),
                  FAKE_EXIT: _shell_output(f"Exit code: 0\n{PASS_OUT}", None),
                  SUCCESS: _shell_output(PASS_OUT, 0)}
        end = {}
    if shape != NO_RESULT:
        records.append(clock.line("response_item", {
            "type": "custom_tool_call_output" if claim == FILE else "function_call_output",
            "call_id": "p1", "output": output[shape]}))
        if shape in end:
            records.append(clock.line("event_msg", {"type": "patch_apply_end",
                                                    "call_id": "p1", **end[shape]}))
    records.append(clock.line("response_item", mc.message("assistant", CLAIM_TEXT[claim])))
    path = project.parent / "log" / "rollout-2026-06-10T12-00-00-conformance.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(mc.write(path, records))
