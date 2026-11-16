"""Synthetic opencode databases for the opencode reader tests. Never a real log.

The tables follow opencode's own definitions, with only the columns the
reader touches plus the NOT NULL ones:
- v1: tables session / message / part, from packages/core/src/session/sql.ts
  and packages/schema/src/v1/session.ts at release v1.19.5 (commit
  810c0eba1a7348036f8bdf61e6632d0c36c6876c);
- v2: tables session_v2 / session_message / session_part, from
  packages/core/src/session/sql.ts and packages/schema/src/session-message.ts
  on branch dev (commit c5e741e787594e8c18fa70ab3a4705db1c927793).
github.com/anomalyco/opencode, MIT. Only field names are taken from there;
every value here is made up. Built at test time, so no .db file is kept.

Run: python tests/fixtures/opencode/make_opencode.py OUT.db  (writes the v2 demo).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

CWD = "/home/dev/acme-api"
T0 = 1781092800000  # 2026-06-10T12:00:00Z in ms

_V1_SQL = """
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, slug TEXT NOT NULL,
  directory TEXT NOT NULL, title TEXT NOT NULL, version TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
"""
_V2_SQL = """
CREATE TABLE session_v2 (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, slug TEXT NOT NULL,
  directory TEXT NOT NULL, version TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
  seq INTEGER NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
  data TEXT NOT NULL);
CREATE TABLE session_part (message_id TEXT NOT NULL, session_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL, type TEXT NOT NULL, call_id TEXT,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY (message_id, ordinal));
"""
SHELL_TOOL = {"v1": "bash", "v2": "shell"}
PATH_KEY = {"v1": "filePath", "v2": "path"}
PATCH_TOOL = {"v1": "apply_patch", "v2": "patch"}


class Builder:
    """Writes one session at a time into a v1 or v2 opencode database."""

    def __init__(self, path, schema: str = "v2"):
        self.path = str(path)
        self.schema = schema
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(_V2_SQL if schema == "v2" else _V1_SQL)
        self.clock = T0
        self.n = 0
        self.sid = ""

    def _tick(self) -> int:
        self.clock += 1000
        self.n += 1
        return self.clock

    def session(self, sid: str, directory: str = CWD, updated: int | None = None) -> "Builder":
        self.sid = sid
        now = updated or self._tick()
        if self.schema == "v2":
            self.conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
                              (sid, "proj_1", "demo", directory, "2.3.0", now, now))
        else:
            self.conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
                              (sid, "proj_1", "demo", directory, "demo", "1.19.5", now, now))
        return self

    # messages

    def _message(self, mtype: str, data: dict) -> str:
        now = self._tick()
        mid = f"msg_{self.n:06d}"
        if self.schema == "v2":
            self.conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                              (mid, self.sid, mtype, self.n, now, now, json.dumps(data)))
        else:
            self.conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                              (mid, self.sid, now, now, json.dumps(data)))
        return mid

    def _part(self, mid: str, part: dict, ordinal: int = 0) -> None:
        now = self.clock
        if self.schema == "v2":
            self.conn.execute("INSERT INTO session_part VALUES (?,?,?,?,?,?,?,?)",
                              (mid, self.sid, ordinal, part.get("type", "?"),
                               part.get("id"), now, now, json.dumps(part)))
        else:
            self.n += 1
            self.conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                              (f"prt_{self.n:06d}", mid, self.sid, now, now, json.dumps(part)))

    def user(self, text: str) -> "Builder":
        if self.schema == "v2":
            self._message("user", {"text": text, "time": {"created": self.clock}})
        else:
            mid = self._message("user", {"role": "user", "time": {"created": self.clock},
                                         "agent": "build"})
            self._part(mid, {"type": "text", "text": text})
        return self

    def _assistant(self) -> str:
        if self.schema == "v2":
            return self._message("assistant", {"agent": "build", "time": {"created": self.clock}})
        return self._message("assistant", {
            "role": "assistant", "time": {"created": self.clock}, "parentID": "msg_0",
            "modelID": "m", "providerID": "p", "mode": "build", "agent": "build",
            "path": {"cwd": CWD, "root": CWD}, "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}})

    def say(self, text: str) -> "Builder":
        self._part(self._assistant(), {"type": "text", "text": text})
        return self

    def raw_part(self, part: dict) -> "Builder":
        self._part(self._assistant(), part)
        return self

    def tool(self, name: str, args: dict, status: str = "completed", output: str = "",
             metadata: dict | None = None, error: str = "", call_id: str = "") -> "Builder":
        call_id = call_id or f"call_{self.n + 1:04d}"
        state: dict = {"status": status, "input": args}
        if self.schema == "v2":
            if status == "completed":
                state["content"] = [{"type": "text", "text": output or "(no output)"}]
            if status == "error":
                state["error"] = {"type": "tool", "message": error}
            if metadata is not None:
                state["metadata"] = metadata
            part = {"type": "tool", "id": call_id, "name": name, "state": state,
                    "time": {"created": self.clock}}
        else:
            if status == "completed":
                state.update(output=output, title=name, metadata=metadata or {},
                             time={"start": self.clock, "end": self.clock})
            elif status == "error":
                state.update(error=error, time={"start": self.clock, "end": self.clock})
                if metadata is not None:
                    state["metadata"] = metadata
            elif status == "pending":
                state["raw"] = ""
            part = {"type": "tool", "callID": call_id, "tool": name, "state": state}
        return self.raw_part(part)

    def shell(self, command: str, output: str = "", exit: int | None = 0,
              status: str = "completed", workdir: str = "") -> "Builder":
        args = {"command": command, **({"workdir": workdir} if workdir else {})}
        meta = None if status in ("pending", "running", "streaming") else (
            {"exit": exit, "output": output} if exit is not None else {"output": output})
        if self.schema == "v2" and meta is not None:
            meta["status"] = "completed"
        return self.tool(SHELL_TOOL[self.schema], args, status, output, meta)

    def write(self, path: str, content: str) -> "Builder":
        return self.tool("write", {PATH_KEY[self.schema]: path, "content": content},
                         output="Wrote file successfully.")

    def edit(self, path: str, old: str, new: str) -> "Builder":
        return self.tool("edit", {PATH_KEY[self.schema]: path, "oldString": old,
                                  "newString": new}, output="Edit applied successfully.")

    def user_shell(self, command: str, status: str = "exited", exit: int | None = 0,
                   output: str = "") -> "Builder":
        data = {"shellID": f"sh_{self.n}", "command": command, "status": status,
                "time": {"created": self.clock}}
        if exit is not None:
            data["exit"] = exit
        if output:
            data["output"] = {"output": output, "cursor": len(output), "size": len(output),
                              "truncated": False}
        self._message("shell", data)
        return self

    def raw_message(self, mtype: str, raw: str) -> "Builder":
        now = self._tick()
        self.conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                          (f"msg_{self.n:06d}", self.sid, mtype, self.n, now, now, raw))
        return self

    def close(self) -> str:
        self.conn.commit()
        self.conn.close()
        return self.path


def demo(path, schema: str = "v2", sid: str = "ses_demo0001") -> str:
    """Edit, write, a passing test run, a commit, then the closing claims."""
    b = Builder(path, schema).session(sid)
    b.user("Add rate limiting to the API.")
    b.say("I'll add rate limiting to the API and make sure everything passes.")
    b.edit(f"{CWD}/src/middleware/rate_limit.py", "# TODO: rate limiting",
           "limiter = RateLimiter(max_requests=100)")
    b.write(f"{CWD}/tests/test_rate_limit.py", "def test_limit():\n    assert True\n")
    b.shell("python -m pytest -q", "12 passed in 0.41s", 0)
    b.shell("git commit -am 'Add rate limiting'", "[main 1a2b3c4] Add rate limiting", 0)
    b.say("All tests pass. I committed the changes.")
    return b.close()


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("opencode-demo.db")
    print(demo(out))
