"""Build synthetic Goose sessions.db files for the tests (stdlib sqlite3 only).

The table definitions are adapted from Goose's own schema (Apache License 2.0;
see LICENSE-goose.txt in this folder): github.com/aaif-goose/goose,
commit d8de0e789f1931cd54d6764dee931166b6c091ca,
crates/goose/src/session/session_manager.rs L1157-1225 (schema_version,
sessions, messages; CURRENT_SCHEMA_VERSION 18 at L33). Changed from the
original: reformatted, IF NOT EXISTS removed. Message blocks follow
crates/goose-provider-types/src/conversation/message.rs and
tool_result_serde.rs at the same commit; the developer shell result follows
crates/goose/src/agents/platform_extensions/developer/shell.rs (ShellOutput,
L191-210; isError and "Command exited with code N", L339-381).

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

T0 = 1_781_092_800  # 2026-06-10T12:00:00Z, in seconds like Goose's created_timestamp
CWD = "/home/dev/acme-api"

SCHEMA = [
    """CREATE TABLE schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE sessions (
        id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
        user_set_name BOOLEAN DEFAULT FALSE, session_type TEXT NOT NULL DEFAULT 'user',
        working_dir TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, extension_data TEXT DEFAULT '{}',
        total_tokens INTEGER, input_tokens INTEGER, output_tokens INTEGER,
        cache_read_tokens INTEGER, cache_write_tokens INTEGER, accumulated_total_tokens INTEGER,
        accumulated_input_tokens INTEGER, accumulated_output_tokens INTEGER,
        accumulated_cache_read_tokens INTEGER, accumulated_cache_write_tokens INTEGER,
        accumulated_cost REAL, schedule_id TEXT, recipe_json TEXT, user_recipe_values_json TEXT,
        provider_name TEXT, model_config_json TEXT, goose_mode TEXT NOT NULL DEFAULT 'auto',
        archived_at TIMESTAMP, project_id TEXT, parent_session_id TEXT,
        system_prompt_override TEXT, system_prompt_extras_json TEXT, container_id TEXT)""",
    """CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT,
        session_id TEXT NOT NULL REFERENCES sessions(id), role TEXT NOT NULL,
        content_json TEXT NOT NULL, created_timestamp INTEGER NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, tokens INTEGER, metadata_json TEXT)""",
]


def shell_result(output: str, exit=0, is_error=None, timed_out=False, structured=True) -> dict:
    """A developer shell CallToolResult the way shell.rs builds it."""
    if is_error is None:
        is_error = timed_out or exit is None or exit != 0
    text = output
    if is_error and isinstance(exit, int) and exit != 0:
        text += f"\n\nCommand exited with code {exit}"
    value = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured:
        out = {"stdout": output, "stderr": ""}
        if exit is not None:
            out["exit_code"] = exit
        if timed_out:
            out["timed_out"] = True
        value["structuredContent"] = out
    return value


class Builder:
    def __init__(self, path, version: int = 18):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        for statement in SCHEMA:
            self.conn.execute(statement)
        self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        self.sid = ""
        self.clock = T0
        self.calls = 0

    def session(self, sid: str, cwd: str = CWD, updated: str = "2026-06-10 12:00:00",
                session_type: str = "user") -> "Builder":
        self.conn.execute(
            "INSERT INTO sessions (id, working_dir, updated_at, session_type) VALUES (?, ?, ?, ?)",
            (sid, cwd, updated, session_type))
        self.sid = sid
        return self

    def raw(self, role: str, content_json: str, metadata: dict | None = None,
            created: int | None = None) -> int:
        self.clock += 1
        cur = self.conn.execute(
            "INSERT INTO messages (session_id, role, content_json, created_timestamp, metadata_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (self.sid, role, content_json, self.clock if created is None else created,
             json.dumps(metadata) if metadata is not None else None))
        return cur.lastrowid

    def message(self, role: str, blocks: list, metadata: dict | None = None) -> int:
        return self.raw(role, json.dumps(blocks), metadata)

    def user(self, text: str, metadata: dict | None = None) -> int:
        return self.message("user", [{"type": "text", "text": text}], metadata)

    def say(self, text: str) -> int:
        return self.message("assistant", [{"type": "text", "text": text}])

    def request(self, name: str, arguments: dict, call_id: str | None = None,
                meta: dict | None = None) -> str:
        self.calls += 1
        call_id = call_id or f"call_{self.calls:03d}"
        block = {"type": "toolRequest", "id": call_id,
                 "toolCall": {"status": "success", "value": {"name": name, "arguments": arguments}}}
        if meta is not None:
            block["_meta"] = meta
        self.message("assistant", [block])
        return call_id

    def respond(self, call_id: str, value=None, error: str | None = None) -> int:
        result = ({"status": "error", "error": error} if error is not None
                  else {"status": "success", "value": value})
        return self.message("user", [{"type": "toolResponse", "id": call_id, "toolResult": result}])

    def tool(self, name: str, arguments: dict, value=None, error: str | None = None,
             respond: bool = True) -> str:
        call_id = self.request(name, arguments)
        if respond:
            self.respond(call_id, value if value is not None or error is not None
                         else {"content": [{"type": "text", "text": "ok"}], "isError": False},
                         error)
        return call_id

    def shell(self, command: str, output: str = "", exit=0, name: str = "developer__shell",
              **kw) -> str:
        return self.tool(name, {"command": command}, shell_result(output, exit, **kw))

    def write(self, path: str, content: str) -> str:
        return self.tool("developer__write", {"path": path, "content": content})

    def edit(self, path: str, before: str, after: str) -> str:
        return self.tool("developer__edit", {"path": path, "before": before, "after": after})

    def close(self) -> str:
        self.conn.commit()
        self.conn.close()
        return str(self.path)


def demo(path, version: int = 18) -> str:
    """One session: edit, a passing test run (exit 0), a commit, then the claims."""
    b = Builder(path, version).session("20260610_1")
    b.user("Fix the parser and run the tests.")
    b.edit(CWD + "/src/parser.py", "return None", "return value")
    b.shell("pytest -q", "12 passed in 0.41s", 0)
    b.shell("git commit -am 'fix parser'", "[main abc1234] fix parser", 0)
    b.say("All tests pass. I committed the changes.")
    return b.close()
