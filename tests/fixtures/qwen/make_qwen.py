"""Synthetic Qwen Code chat files for the Qwen reader tests. Never a real log.

Shapes follow github.com/QwenLM/qwen-code (Apache-2.0) at commit
516e963f7a401053c18c23b32b007ec1fb70848f:
- packages/core/src/services/chatRecordingService.ts: ChatRecord {uuid,
  parentUuid, sessionId, timestamp, type, subtype?, cwd, version, message:
  {role, parts}, toolCallResult?}; one JSON object per line in
  <runtime>/projects/<project id>/chats/<session id>.jsonl; a rewind re-roots
  the parentUuid chain (rewindRecording);
- packages/core/src/config/storage.ts and utils/paths.ts sanitizeCwd: the
  project id is the project path with non-alphanumerics made "-";
- packages/core/src/core/coreToolScheduler.ts: a result part is
  {"functionResponse": {"id", "name", "response": {"output"}}}, an error one
  has "response": {"error"};
- packages/core/src/tools/shell.ts L4190-4200 and utils/shell-result.ts: the
  shell result text (Command, Directory, Output, Error, Exit Code on every
  finished run, Signal, Process Group PGID) and the structured resultDisplay
  {type: "shell_result", version: 1, exitCode, outcome, ...}.
All text, ids and paths here are made up.

The demo is the Claude Code demo (examples/make_demo.py) in Qwen's shape: the
same edits, the same commands and results, the same closing claim. Qwen logs
the exit code of every finished run, 0 included.

Run: python tests/fixtures/qwen/make_qwen.py OUT_DIR  (writes the demo).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CWD = "/home/dev/acme-api"
SID = "7a2b3c4d-0000-4000-8000-00000000demo"
TS = "2026-06-10T12:00:{:02d}.000Z"
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


class Chat:
    """Records chained through parentUuid, as the recorder appends them."""

    def __init__(self, session_id: str = SID, cwd: str = CWD):
        self.session_id = session_id
        self.cwd = cwd
        self.second = 0
        self.last: str | None = None
        self.records: list[dict] = []
        self.n = 0

    def base(self, kind: str, parent: str | None | bool = True) -> dict:
        self.second += 1
        self.n += 1
        uuid = f"r{self.n:04d}"
        record = {"uuid": uuid, "parentUuid": self.last if parent is True else parent,
                  "sessionId": self.session_id, "timestamp": TS.format(self.second % 60),
                  "type": kind, "cwd": self.cwd, "version": "0.0.0-test"}
        self.records.append(record)
        self.last = uuid
        return record

    def user(self, text: str) -> dict:
        record = self.base("user")
        record["message"] = {"role": "user", "parts": [{"text": text}]}
        return record

    def assistant(self, text: str = "", calls: list[dict] | None = None,
                  thought: str = "") -> dict:
        parts = [{"text": thought, "thought": True}] if thought else []
        if text:
            parts.append({"text": text})
        for call in calls or []:
            parts.append({"functionCall": call})
        record = self.base("assistant")
        record["model"] = "test-model"
        record["message"] = {"role": "model", "parts": parts}
        return record

    def result(self, call_id: str, name: str, output: str | None = None,
               error: str | None = None, status: str = "success",
               display=None) -> dict:
        response = {"error": error} if error is not None else {"output": output or ""}
        record = self.base("tool_result")
        record["message"] = {"role": "user", "parts": [
            {"functionResponse": {"id": call_id, "name": name, "response": response}}]}
        info = {"callId": call_id, "status": status}
        if display is not None:
            info["resultDisplay"] = display
        record["toolCallResult"] = info
        return record

    def system(self, subtype: str, payload: dict | None = None) -> dict:
        record = self.base("system")
        record["subtype"] = subtype
        record["systemPayload"] = payload or {}
        return record


def call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "name": name, "args": args}


def shell_text(command: str, output: str, code: int | None, directory: str = "") -> str:
    """run_shell_command's result text, as shell.ts L4190-4200 builds it."""
    return "\n".join([
        f"Command: {command}",
        f"Directory: {directory or '(root)'}",
        f"Output: {output or '(empty)'}",
        "Error: (none)",
        f"Exit Code: {code if code is not None else '(none)'}",
        "Signal: (none)",
        "Process Group PGID: 4242",
    ])


def shell_display(output: str, code: int | None, outcome: str | None = None) -> dict:
    """The structured resultDisplay (utils/shell-result.ts ShellResultDisplay)."""
    if outcome is None:
        outcome = "completed" if not code else "failed"
    return {"type": "shell_result", "version": 1, "text": output, "output": output,
            "directory": CWD, "exitCode": code, "signal": None, "pid": 4242,
            "error": None, "outcome": outcome, "notices": [], "truncated": False,
            "outputFiles": []}


def shell(chat: Chat, call_id: str, command: str, code: int | None, out: str,
          structured: bool = True, text: str = "") -> None:
    chat.assistant(calls=[call(call_id, "run_shell_command",
                               {"command": command, "is_background": False})])
    status = "error" if code else "success"
    body = text or shell_text(command, out, code)
    display = shell_display(out, code) if structured else out
    if status == "error":
        chat.result(call_id, "run_shell_command", error=body, status=status, display=display)
    else:
        chat.result(call_id, "run_shell_command", output=body, status=status, display=display)


def edit(chat: Chat, call_id: str, path: str, old: str, new: str) -> None:
    chat.assistant(calls=[call(call_id, "edit",
                               {"file_path": path, "old_string": old, "new_string": new})])
    chat.result(call_id, "edit", output=f"The file: {path} has been updated.")


def demo() -> list[dict]:
    chat = Chat()
    chat.system("session_source", {"source": "cli"})
    chat.user(PROMPT)
    chat.assistant(OPENING, thought="Plan the change.")
    edit(chat, "c1", f"{CWD}/src/middleware/rate_limit.py",
         "# TODO: rate limiting", "limiter = RateLimiter(max_requests=100)")
    shell(chat, "c2", *RUNS[0])
    edit(chat, "c3", f"{CWD}/tests/test_rate_limit.py",
         "    assert resp.status_code == 429\n    assert 'Retry-After' in resp.headers",
         "    assert resp.status_code in (200, 429)")
    shell(chat, "c4", *RUNS[1])
    shell(chat, "c5", *RUNS[2])
    chat.assistant(CLOSING)
    return chat.records


def project_id(cwd: str) -> str:
    """utils/paths.ts sanitizeCwd (POSIX case)."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def write(path: Path, records: list[dict]) -> Path:
    data = ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def place(home: Path, records: list[dict], session_id: str = SID, cwd: str = CWD,
          group: str = "projects", folder: str | None = None) -> Path:
    """Write a session where Qwen keeps it: <home>/projects/<project id>/chats/."""
    name = folder if folder is not None else project_id(cwd)
    return write(home / group / name / "chats" / f"{session_id}.jsonl", records)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    print(f"wrote {write(out / f'{SID}.jsonl', demo())}")
