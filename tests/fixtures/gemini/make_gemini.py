"""Synthetic Gemini CLI chat files for the Gemini reader tests. Never a real log.

Shapes follow github.com/google-gemini/gemini-cli (Apache-2.0) at commit
4ed06ed0ddc523a6d356a4ad35ddfea331041888:
- packages/core/src/services/chatRecordingTypes.ts: the metadata line,
  MessageRecord, ToolCallRecord, $rewindTo / $patch / $set records;
- packages/core/src/services/chatRecordingService.ts: one JSON object per
  line, a message appended again (same id) each time it changes, file name
  session-<YYYY-MM-DDTHH-MM>-<first 8 of the session id>.jsonl under
  tmp/<project id>/chats/, ".project_root" beside "chats";
- packages/core/src/utils/generateContentResponseUtilities.ts: a result is
  [{"functionResponse": {"id", "name", "response": {"output"}}}];
- packages/core/src/scheduler/tool-executor.ts: an error result has
  "response": {"error"};
- packages/core/src/tools/shell.ts and utils/textUtils.ts wrapUntrusted: the
  shell output text ("Output: ...", "Exit Code: N" only when N is not 0,
  "Process Group PGID: N"), wrapped in <untrusted_context>.
All text, ids and paths here are made up.

The demo is the Claude Code demo (examples/make_demo.py) in Gemini's shape:
the same edits, the same commands and results, the same closing claim. Only
the failing run carries an exit code: Gemini never writes one for a pass.

Run: python tests/fixtures/gemini/make_gemini.py OUT_DIR  (writes the demo).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CWD = "/home/dev/acme-api"
SID = "6f1c2d3e-0000-4000-8000-00000000demo"
HASH = "0" * 64  # a stand-in for sha256(project root)
TS = "2026-06-10T12:00:{:02d}.000Z"
NAME = "session-2026-06-10T12-00-{}.jsonl"
PROMPT = "Add rate limiting to the API."
OPENING = "I'll add rate limiting to the API and make sure everything passes."
CLOSING = ("All tests pass. I committed the changes, the rate limiting "
           "feature is complete and everything is working.")
# (command, exit code or None, output) of each shell run, as in the Claude demo
RUNS = [
    ("python -m pytest tests/ -q", 1, "2 failed, 14 passed in 1.2s"),
    ("python -m pytest tests/ -q || true", None, "1 failed, 15 passed in 1.1s"),
    ("git commit -am 'Add rate limiting' --no-verify", None, "[main 4f2a9c1] Add rate limiting"),
]


class Clock:
    def __init__(self):
        self.second = 0

    def now(self) -> str:
        self.second += 1
        return TS.format(self.second % 60)


def meta(session_id: str = SID, project_hash: str = HASH) -> dict:
    return {"sessionId": session_id, "projectHash": project_hash,
            "startTime": TS.format(0), "lastUpdated": TS.format(0)}


def user(clock: Clock, mid: str, text: str) -> dict:
    return {"id": mid, "timestamp": clock.now(), "type": "user", "content": [{"text": text}]}


def gemini(clock: Clock, mid: str, text: str = "", tool_calls: list | None = None) -> dict:
    record = {"id": mid, "timestamp": clock.now(), "type": "gemini", "content": text,
              "thoughts": [], "tokens": None, "model": "test-model"}
    if tool_calls is not None:
        record["toolCalls"] = tool_calls
    return record


def shell_text(output: str, code: int | None = None, pgid: int | None = 4242) -> str:
    """run_shell_command's result text, as shell.ts builds it."""
    lines = [f"Output: {output or '(empty)'}"]
    if code:
        lines.append(f"Exit Code: {code}")
    if pgid:
        lines.append(f"Process Group PGID: {pgid}")
    return "<untrusted_context>\n" + "\n".join(lines) + "\n</untrusted_context>"


def output(call_id: str, name: str, text: str) -> list:
    return [{"functionResponse": {"id": call_id, "name": name, "response": {"output": text}}}]


def error(call_id: str, name: str, text: str) -> list:
    return [{"functionResponse": {"id": call_id, "name": name, "response": {"error": text}}}]


def call(clock: Clock, call_id: str, name: str, args: dict, status: str = "scheduled",
         result: list | None = None) -> dict:
    record = {"id": call_id, "name": name, "args": args, "status": status,
              "timestamp": clock.now()}
    if result is not None:
        record["result"] = result
    return record


def shell_call(clock: Clock, call_id: str, command: str, code: int | None, out: str,
               status: str = "success") -> dict:
    return call(clock, call_id, "run_shell_command", {"command": command}, status,
                output(call_id, "run_shell_command", shell_text(out, code)))


def edit_call(clock: Clock, call_id: str, path: str, old: str, new: str,
              status: str = "success") -> dict:
    return call(clock, call_id, "replace",
                {"file_path": path, "old_string": old, "new_string": new}, status,
                output(call_id, "replace", f"Successfully modified file: {path} (1 replacements)."))


def demo() -> list[dict]:
    clock = Clock()
    records = [meta(), user(clock, "u1", PROMPT), gemini(clock, "g1", OPENING)]
    steps = [
        ("c1", lambda: edit_call(clock, "c1", f"{CWD}/src/middleware/rate_limit.py",
                                   "# TODO: rate limiting",
                                   "limiter = RateLimiter(max_requests=100)")),
        ("c2", lambda: shell_call(clock, "c2", *RUNS[0])),
        ("c3", lambda: edit_call(clock, "c3", f"{CWD}/tests/test_rate_limit.py",
                                   "    assert resp.status_code == 429\n"
                                   "    assert 'Retry-After' in resp.headers",
                                   "    assert resp.status_code in (200, 429)")),
        ("c4", lambda: shell_call(clock, "c4", *RUNS[1])),
        ("c5", lambda: shell_call(clock, "c5", *RUNS[2])),
    ]
    for n, (call_id, done) in enumerate(steps, start=2):
        mid = f"g{n}"
        finished = done()
        scheduled = {k: v for k, v in finished.items() if k != "result"}
        scheduled["status"] = "scheduled"
        message = gemini(clock, mid, "", [scheduled])
        records.append(message)  # written when the call is scheduled
        records.append({**message, "toolCalls": [finished]})  # again with its result
    records.append(gemini(clock, "g9", CLOSING))
    return records


def write(path: Path, records: list[dict]) -> Path:
    data = ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def place(home: Path, records: list[dict], short: str = "6f1c2d3e", slug: str = "acme-api",
          project: str | None = CWD, minute: str = "00") -> Path:
    """Write a session where Gemini keeps it: <home>/.gemini/tmp/<slug>/chats/."""
    folder = home / ".gemini" / "tmp" / slug
    if project is not None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".project_root").write_bytes(project.encode("utf-8"))
    name = f"session-2026-06-10T12-{minute}-{short}.jsonl"
    return write(folder / "chats" / name, records)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    print(f"wrote {write(out / NAME.format('6f1c2d3e'), demo())}")
