"""Claude Code conformance builder (JSONL: tool_use, then tool_result with is_error)."""

from __future__ import annotations

import json
from pathlib import Path

from _common import (CANCELLED, CLAIM_TEXT, CONTENT, FAILED, FAKE_EXIT, FILE, NO_RESULT,
                     PASS_OUT, PROMPT, REWOUND, SKIPPED, SUCCESS, TEST_CMD, TESTS, target)

AGENT = "claude-code"
_OPEN = ("open (D087): /rewind leaves the old branch in the JSONL (parentUuid); "
         "the parser reads every record in file order and has no rewind model yet")
CANNOT = {(REWOUND, FILE): _OPEN, (REWOUND, TESTS): _OPEN}

_DENIED = ("The user doesn't want to proceed with this tool use. The tool use was rejected "
           "(eg. if it was a file edit, the new_string was NOT written to the file).")
_INTERRUPTED = "[Request interrupted by user for tool use]"


def _record(kind: str, cwd: str, second: int, content: list, **extra) -> dict:
    role = "assistant" if kind == "assistant" else "user"
    return {"type": kind, "timestamp": f"2026-06-10T12:00:{second:02d}.000Z",
            "sessionId": "conformance", "cwd": cwd,
            "message": {"role": role, "content": content}, **extra}


def build(project: Path, shape: str, claim: str) -> str:
    cwd = str(project)
    if claim == FILE:
        name, tool_input = "Write", {"file_path": target(project), "content": CONTENT}
        ok_text = f"File created successfully at: {target(project)}"
        fail_text = "<tool_use_error>File has not been read yet.</tool_use_error>"
        fake_text = f"Exit code 0\n{ok_text}"
    else:
        name, tool_input = "Bash", {"command": TEST_CMD}
        ok_text = PASS_OUT
        fail_text = "Error: Exit code 1\n1 failed, 15 passed in 1.1s"
        fake_text = f"{PASS_OUT}\nExit code 0"
    records = [_record("user", cwd, 0, [{"type": "text", "text": PROMPT}]),
               _record("assistant", cwd, 1, [{"type": "tool_use", "id": "t1", "name": name,
                                               "input": tool_input}])]
    result = {SKIPPED: (_DENIED, True), CANCELLED: (_INTERRUPTED, True),
              FAILED: (fail_text, True), FAKE_EXIT: (fake_text, True),
              SUCCESS: (ok_text, False)}.get(shape)
    if shape != NO_RESULT:
        text, is_error = result
        use = text if is_error else {"stdout": text, "stderr": "", "interrupted": False}
        records.append(_record("user", cwd, 2, [{"type": "tool_result", "tool_use_id": "t1",
                                                  "content": text, "is_error": is_error}],
                               toolUseResult=use))
    records.append(_record("assistant", cwd, 3, [{"type": "text", "text": CLAIM_TEXT[claim]}]))
    path = project.parent / "log" / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return str(path)
