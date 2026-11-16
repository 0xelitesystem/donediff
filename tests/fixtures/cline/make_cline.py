"""Synthetic Cline session files for the Cline reader tests. Never a real log.

Shapes follow github.com/cline/cline (Apache-2.0) at commit
146894b6a42a7a554c6ff37f7bb33fe5122cc33d:
- sdk/packages/core/docs/messages-contract-v1.md and services/session-data.ts
  buildMessagesFilePayload: {version: 1, updated_at, agent, sessionId,
  messages: [{id, role, content: [blocks], ts}], system_prompt?}, written
  pretty-printed to <sessions>/<id>/<id>.messages.json;
- services/session-artifacts.ts and session/models/session-manifest.ts: the
  manifest <id>.json beside it, with cwd and workspace_root;
- extensions/tools/definitions.ts executeShellCommands and executors/bash.ts:
  a run_commands result is a list of {query, result, success}; a non-zero
  exit adds error "Command exited with code N" and puts "[Command exited with
  code N]" before the output; exit 0 writes no code;
- definitions.ts createEditorTool / createApplyPatchTool: one {query, result,
  success} JSON-stringified (session/persisted-tool-result-content.ts).
All text, ids and paths here are made up.

The demo is the Claude Code demo (examples/make_demo.py) in Cline's shape: the
same edits, the same commands and results, the same closing claim. Cline
writes no exit code for a passing command, so those runs are status only.

Run: python tests/fixtures/cline/make_cline.py OUT_DIR  (writes the demo).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CWD = "/home/dev/acme-api"
SID = "1781000000000_demo1"
T0 = 1_781_092_800_000  # epoch ms
PROMPT = "Add rate limiting to the API."
OPENING = "I'll add rate limiting to the API and make sure everything passes."
CLOSING = ("All tests pass. I committed the changes, the rate limiting "
           "feature is complete and everything is working.")
# (command, exit code, output) of each shell run, as in the Claude demo
RUNS = [
    ("python -m pytest tests/ -q", 1, "2 failed, 14 passed in 1.2s"),
    ("python -m pytest tests/ -q || true", 0, "1 failed, 15 passed in 1.1s"),
    ("git commit -am 'Add rate limiting' --no-verify", 0, "[main 4f2a9c1] Add rate limiting"),
]


class Doc:
    """A messages file, built message by message."""

    def __init__(self, session_id: str = SID, cwd: str = CWD):
        self.session_id = session_id
        self.cwd = cwd
        self.messages: list[dict] = []
        self.n = 0

    def add(self, role: str, content: list) -> dict:
        self.n += 1
        message = {"id": f"msg_{self.n:04d}", "role": role, "content": content,
                   "ts": T0 + self.n * 1000}
        self.messages.append(message)
        return message

    def user(self, text: str) -> dict:
        return self.add("user", [{"type": "text", "text": text}])

    def assistant(self, text: str = "", calls: list[dict] | None = None,
                  thinking: str = "") -> dict:
        content = [{"type": "thinking", "thinking": thinking}] if thinking else []
        if text:
            content.append({"type": "text", "text": text})
        content.extend(calls or [])
        message = self.add("assistant", content)
        message["modelInfo"] = {"id": "test-model", "provider": "test"}
        return message

    def results(self, *blocks: dict) -> dict:
        return self.add("user", list(blocks))

    def document(self, **extra) -> dict:
        doc = {"version": 1, "updated_at": "2026-06-10T12:00:59.000Z", "agent": "lead",
               "sessionId": self.session_id, "messages": self.messages}
        doc.update(extra)
        return doc


def call(call_id: str, name: str, tool_input) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


def result(call_id: str, name: str, content, is_error: bool | None = None) -> dict:
    block = {"type": "tool_result", "tool_use_id": call_id, "name": name, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return block


def command_op(command: str, code: int | None, out: str, error: str = "") -> dict:
    """One run_commands entry, as executeShellCommands builds it."""
    if code == 0:
        return {"query": command, "result": out, "success": True}
    if code is not None:
        notice = f"[Command exited with code {code}]"
        return {"query": command, "result": f"{notice}\n{out}" if out else notice,
                "error": f"Command exited with code {code}", "success": False}
    return {"query": command, "result": "", "error": error or "Command failed: x",
            "success": False}


def op(query: str, text: str, success: bool = True) -> str:
    """An editor / apply_patch result, JSON-stringified as Cline stores it."""
    body = {"query": query, "result": text if success else "", "success": success}
    if not success:
        body["error"] = text
    return json.dumps(body)


def shell(doc: Doc, call_id: str, command: str, code: int | None, out: str) -> None:
    doc.assistant(calls=[call(call_id, "run_commands", {"commands": [command]})])
    doc.results(result(call_id, "run_commands", [command_op(command, code, out)]))


def edit(doc: Doc, call_id: str, path: str, old: str, new: str) -> None:
    doc.assistant(calls=[call(call_id, "editor",
                              {"path": path, "old_text": old, "new_text": new})])
    doc.results(result(call_id, "editor", op(f"edit:{path}", f"Edited {path}")))


def demo() -> dict:
    doc = Doc()
    doc.user(PROMPT)
    doc.assistant(OPENING, thinking="Plan the change.")
    edit(doc, "toolu_c1", f"{CWD}/src/middleware/rate_limit.py",
         "# TODO: rate limiting", "limiter = RateLimiter(max_requests=100)")
    shell(doc, "toolu_c2", *RUNS[0])
    edit(doc, "toolu_c3", f"{CWD}/tests/test_rate_limit.py",
         "    assert resp.status_code == 429\n    assert 'Retry-After' in resp.headers",
         "    assert resp.status_code in (200, 429)")
    shell(doc, "toolu_c4", *RUNS[1])
    shell(doc, "toolu_c5", *RUNS[2])
    doc.assistant(CLOSING)
    return doc.document()


def write(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(doc, indent=2) + "\n").encode("utf-8"))
    return path


def manifest(session_id: str, cwd: str) -> dict:
    return {"version": 1, "session_id": session_id, "source": "cli", "pid": 1,
            "started_at": "2026-06-10T12:00:00.000Z", "status": "completed",
            "interactive": True, "provider": "test", "model": "test-model",
            "cwd": cwd, "workspace_root": cwd, "enable_tools": True,
            "enable_spawn": False, "enable_teams": False}


def place(sessions: Path, doc: dict, cwd: str | None = CWD) -> Path:
    """Write a session where Cline keeps it: <sessions>/<id>/<id>.messages.json (+ manifest)."""
    sid = doc["sessionId"]
    folder = sessions / sid
    if cwd is not None:
        write(folder / f"{sid}.json", manifest(sid, cwd))
    return write(folder / f"{sid}.messages.json", doc)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    print(f"wrote {write(out / f'{SID}.messages.json', demo())}")
