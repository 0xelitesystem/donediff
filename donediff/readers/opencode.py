"""The opencode reader: sessions in opencode's SQLite database.

Format notes (read from github.com/anomalyco/opencode; the format is
internal and moves). Two schemas are read, both can be in one file:

- v1, release v1.19.5 (commit 810c0eba1a73), packages/core/src/session/sql.ts
  and packages/schema/src/v1/session.ts: tables "session" (id, directory,
  time_created, time_updated), "message" (id, session_id, time_created, data)
  and "part" (id, message_id, session_id, time_created, data). Message data
  has "role" (user / assistant), assistant "path.cwd". Part data has "type":
  "text" (text, synthetic, ignored) or "tool" (callID, tool, state). The tool
  state is pending / running / completed (input, output, metadata) / error
  (input, error, metadata). The shell tool is "bash" (input command, workdir)
  and its metadata.exit is the process exit code (null on timeout or abort).
  "write" (filePath, content), "edit" (filePath, oldString, newString),
  "multiedit" (filePath, edits), "apply_patch" (patchText).
- v2, branch dev (commit c5e741e78759), packages/core/src/session/sql.ts and
  packages/schema/src/session-message.ts: tables "session_v2" (id, directory,
  time_created, time_updated), "session_message" (id, session_id, type, seq,
  time_created, data) and "session_part" (message_id, session_id, ordinal,
  type, call_id, data). Message type "assistant" has its content in
  session_part rows: "text" (text) and "tool" (id, name, state). The tool
  state is streaming / running / completed (input, content[].text, metadata)
  / error (input, error, metadata). The shell tool is "shell" (command,
  workdir) with metadata.exit (packages/core/src/shell/result.ts) and
  metadata.status "running" when it went to the background. "write" and
  "edit" take "path", "patch" takes patchText. Message type "shell" is a
  command the user ran (command, status, exit, output.output).
- Where it lives: $OPENCODE_DB (absolute, or relative to the data folder),
  else <data>/opencode.db, data = $XDG_DATA_HOME/opencode or
  ~/.local/share/opencode, on Windows too (packages/util/src/global-roots.ts,
  packages/cli/src/database-path.ts).

A real exit code comes only from a shell exit field. A call with only a
completed / error state keeps exit_code None and a status, never a made-up
code. The database is opened read-only (SQLite URI mode=ro, query_only) for
the length of one read and closed; nothing is written. A row whose JSON does
not parse, or a part or message type this reader does not know, is skipped
and counted in Session.unrecognised.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat as stat_mod
from datetime import datetime, timezone
from pathlib import Path

from ..evidence import FAILURE_IN_OUTPUT, PASS_IN_OUTPUT, check_types, passed_counts
from ..models import EDIT, MULTI_EDIT, OTHER, SHELL, WRITE, Event, EventKind, Session
from ..shell import is_remote_path
from .base import UnreadableSession
from .codex import clean_path, parse_patch

_MAGIC = b"SQLite format 3\x00"
_TIMEOUT = 1.0  # seconds to wait for a writer's lock before giving up
_V1, _V2 = "v1", "v2"
_TABLES = {_V1: ("session", "message", "part"),
           _V2: ("session_v2", "session_message", "session_part")}
_SHELL_TOOLS = frozenset({"bash", "shell"})
_WRITE_TOOLS = frozenset({"write"})
_EDIT_TOOLS = frozenset({"edit"})
_MULTI_TOOLS = frozenset({"multiedit"})
_PATCH_TOOLS = frozenset({"apply_patch", "patch"})
_V1_PARTS = frozenset({"text", "subtask", "reasoning", "file", "tool", "step-start",
                       "step-finish", "snapshot", "patch", "agent", "retry", "compaction"})
_V2_PARTS = frozenset({"text", "reasoning", "tool"})
_V2_MESSAGES = frozenset({"agent-switched", "model-switched", "location-switched", "user",
                          "synthetic", "system", "skill", "shell", "assistant",
                          "compaction", "idle"})
_WAITING = frozenset({"pending", "running", "streaming"})
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩]")
_ID = re.compile(r"^[0-9A-Za-z_-]{1,128}$")


# -- where the database is ----------------------------------------------------

def database_path() -> Path | None:
    """$OPENCODE_DB when set (None for a remote or in-memory one), else the default file."""
    env = os.environ.get("OPENCODE_DB")
    xdg = os.environ.get("XDG_DATA_HOME")
    data = (Path(xdg) if xdg else Path(os.path.expanduser("~")) / ".local" / "share") / "opencode"
    if env:
        if env == ":memory:" or is_remote_path(env):
            return None
        path = Path(env)
        return path if path.is_absolute() else data / path
    if xdg and is_remote_path(xdg):
        return None
    return data / "opencode.db"


class SessionPath(type(Path())):
    """One opencode session: the database file, plus the session id and schema.

    It stats as a file whose size is the session's stored data and whose
    time is its last update, so `donediff list` and the scan size read right.
    """

    session_id: str = ""
    schema: str = ""
    _stat: os.stat_result | None = None

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        if self._stat is None:
            base = super().stat(follow_symlinks=follow_symlinks)
            size, updated = base.st_size, base.st_mtime
            try:
                with _connect(Path(os.fspath(self))) as conn:
                    size, updated = _session_size(conn, self.schema, self.session_id, base)
            except (sqlite3.Error, UnreadableSession):
                pass
            self._stat = os.stat_result((stat_mod.S_IFREG | 0o444, 0, 0, 1, 0, 0, size,
                                         updated, updated, updated))
        return self._stat

    def label(self) -> str:
        return f"{os.fspath(self)}#{self.session_id}"


def session_path(db: Path, session_id: str, schema: str) -> SessionPath:
    path = SessionPath(db)
    path.session_id = session_id
    path.schema = schema
    return path


class _Conn:
    """A read-only connection, closed on exit (sqlite3's own context manager only commits)."""

    def __init__(self, db: Path):
        self.db = db
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        try:
            uri = Path(os.path.abspath(self.db)).as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, timeout=_TIMEOUT)
            self.conn.execute("PRAGMA query_only = 1")
        except sqlite3.Error as exc:
            if self.conn is not None:
                self.conn.close()
            raise UnreadableSession(_why(exc)) from None
        return self.conn

    def __exit__(self, *exc) -> None:
        if self.conn is not None:
            self.conn.close()


def _connect(db: Path) -> _Conn:
    return _Conn(db)


def _why(exc: sqlite3.Error) -> str:
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "opencode database is locked by another program; try again when it is idle"
    return "opencode database could not be read (damaged, or not an opencode database)"


def is_database(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == _MAGIC
    except OSError:
        return False


def _schemas(conn: sqlite3.Connection) -> list[str]:
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    return [s for s in (_V2, _V1) if all(t in names for t in _TABLES[s])]


def _sessions(conn: sqlite3.Connection) -> list[tuple[str, str, int, str]]:
    """(schema, id, time_updated, directory) of every session, newest first."""
    rows = []
    for schema in _schemas(conn):
        table = _TABLES[schema][0]
        for sid, updated, directory in conn.execute(
                f'SELECT id, time_updated, directory FROM "{table}"'):
            if isinstance(sid, str) and _ID.match(sid):
                rows.append((schema, sid, updated if isinstance(updated, int) else 0,
                             directory if isinstance(directory, str) else ""))
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows


def _session_size(conn, schema: str, sid: str, base) -> tuple[int, float]:
    session, message, part = _TABLES[schema]
    size = 0
    for table in (message, part):
        row = conn.execute(
            f'SELECT COALESCE(SUM(LENGTH(data)), 0) FROM "{table}" WHERE session_id = ?',
            (sid,)).fetchone()
        size += int(row[0] or 0)
    row = conn.execute(f'SELECT time_updated FROM "{session}" WHERE id = ?', (sid,)).fetchone()
    updated = row[0] / 1000 if row and isinstance(row[0], int) else base.st_mtime
    return size, updated


# -- what a row holds ---------------------------------------------------------

def _json(raw) -> dict | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _iso(ms) -> str:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return ""
    try:
        moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _exit(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(str(c.get("text")) for c in content
                     if isinstance(c, dict) and isinstance(c.get("text"), str))


def _error_text(error) -> str:
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        for key in ("message", "data"):
            value = error.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"]
    return ""


def _join(base: str, workdir: str) -> str:
    if not workdir:
        return base
    if workdir.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", workdir) or not base:
        return workdir
    return base.rstrip("/\\") + "/" + workdir


# -- the reader ---------------------------------------------------------------

class _Parse:
    """One read of one session, building the Session."""

    def __init__(self, label: str, sid: str):
        self.session = Session(path=label, session_id=sid, slug=sid[:12])
        self.index = 0
        self.row = 0
        self.clock = float("-inf")
        self.timestamp = ""
        self.cwd = ""

    def at(self, ms) -> None:
        stamp = _iso(ms)
        if not stamp:
            return
        self.timestamp = stamp
        self.session.first_timestamp = self.session.first_timestamp or stamp
        self.session.last_timestamp = stamp
        if ms / 1000 > self.clock:
            self.clock = ms / 1000

    def text(self, text: str) -> None:
        text = text.strip()
        if text:
            self.session.events.append(Event(kind=EventKind.TEXT, index=self.index,
                                             timestamp=self.timestamp, text=text, t=self.clock))
            self.index += 1

    def prompt(self) -> None:
        self.session.prompt_starts.append(self.index)
        self.session.prompt_marks.append((self.index, self.row))

    def _event(self, name: str, action: str, call_id: str, tool_input: dict,
               cwd: str = "") -> Event:
        event = Event(kind=EventKind.TOOL_CALL, index=self.index, timestamp=self.timestamp,
                      tool_name=name, action=action, tool_id=call_id, tool_input=tool_input,
                      t=self.clock, cwd=cwd or self.cwd, result_seen=False)
        if action == SHELL:
            event.check_types = check_types(event.command)
        self.session.events.append(event)
        self.index += 1
        return event

    def tool(self, name: str, call_id: str, state: dict) -> None:
        args = state.get("input")
        args = args if isinstance(args, dict) else {}
        path = clean_path(args.get("filePath") or args.get("path"))
        events: list[Event]
        if name in _SHELL_TOOLS:
            command = _str(args.get("command"))
            events = [self._event(name, SHELL, call_id, {"command": command},
                                  _join(self.cwd, clean_path(args.get("workdir"))))]
        elif name in _WRITE_TOOLS:
            events = [self._event(name, WRITE, call_id,
                                  {"file_path": path, "content": _str(args.get("content"))})]
        elif name in _EDIT_TOOLS:
            events = [self._event(name, EDIT, call_id, {
                "file_path": path, "old_string": _str(args.get("oldString")),
                "new_string": _str(args.get("newString"))})]
        elif name in _MULTI_TOOLS:
            edits = [{"old_string": _str(e.get("oldString")), "new_string": _str(e.get("newString"))}
                     for e in args.get("edits") or [] if isinstance(e, dict)] \
                if isinstance(args.get("edits"), list) else []
            events = [self._event(name, MULTI_EDIT, call_id, {"file_path": path, "edits": edits})]
        elif name in _PATCH_TOOLS:
            events = self.patch(name, call_id, _str(args.get("patchText")))
        else:
            events = [self._event(name, OTHER, call_id, args)]
        self.result(events, state)

    def patch(self, name: str, call_id: str, text: str) -> list[Event]:
        events = []
        for op, path, data in parse_patch(text):
            if op == "add":
                events.append(self._event(name, WRITE, call_id,
                                          {"file_path": path, "content": data}))
            elif op == "update":
                hunks = data or [("", "")]
                if len(hunks) == 1:
                    events.append(self._event(name, EDIT, call_id, {
                        "file_path": path, "old_string": hunks[0][0],
                        "new_string": hunks[0][1]}))
                else:
                    edits = [{"old_string": o, "new_string": n} for o, n in hunks]
                    events.append(self._event(name, MULTI_EDIT, call_id,
                                              {"file_path": path, "edits": edits}))
            else:
                events.append(self._event(name, OTHER, call_id, {"file_path": path}))
        return events or [self._event(name, OTHER, call_id, {})]

    @staticmethod
    def _hints(event: Event) -> None:
        event.failure_hint = bool(FAILURE_IN_OUTPUT.search(event.output))
        event.pass_hint = bool(PASS_IN_OUTPUT.search(event.output))
        event.passed_counts = passed_counts(event.output) if event.check_types else frozenset()

    def result(self, events: list[Event], state: dict) -> None:
        status = _str(state.get("status"))
        if status in _WAITING or status not in ("completed", "error"):
            for event in events:
                event.status = status if status in _WAITING else ""
            return  # no result in the log: result_seen stays False
        meta = state.get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        output = state.get("output") if "output" in state else state.get("content")
        text = _content_text(output)
        if status == "error":
            error = _error_text(state.get("error"))
            text = f"{text}\n{error}".strip() if text else error
        for event in events:
            event.result_seen = True
            event.output = text
            if event.tool_action != SHELL:
                event.status = status
                event.is_error = status == "error"
                continue
            code = _exit(meta.get("exit"))
            if code is not None:
                event.exit_code = code
                event.is_error = code != 0
                event.status = "completed" if code == 0 else "failed"
            elif status == "completed" and meta.get("status") == "running":
                event.background = True  # it went on in the background: no evidence yet
                event.check_types = frozenset()
                event.status = "running"
            else:
                event.status = status  # completed or error, with no exit code recorded
                event.is_error = status == "error"
            if isinstance(meta.get("truncated"), bool):
                event.output_truncated = meta["truncated"]
            self._hints(event)

    def user_shell(self, data: dict) -> None:
        """A v2 "shell" message: a command the user ran, with its own exit field."""
        event = self._event("shell (user)", SHELL, _str(data.get("shellID")),
                            {"command": _str(data.get("command"))})
        status = _str(data.get("status"))
        code = _exit(data.get("exit"))
        out = data.get("output")
        event.output = _str(out.get("output")) if isinstance(out, dict) else ""
        if code is not None:
            event.result_seen = True
            event.exit_code = code
            event.is_error = code != 0
            event.status = "completed" if code == 0 else "failed"
        elif status in ("exited", "timeout", "killed"):
            event.result_seen = True
            event.status = status
            event.is_error = status != "exited"
        else:
            event.status = status
        self._hints(event)


def _read_v1(conn, state: _Parse, sid: str) -> None:
    messages = conn.execute(
        'SELECT id, time_created, data FROM "message" WHERE session_id = ? '
        'ORDER BY time_created, id', (sid,)).fetchall()
    parts: dict[str, list] = {}
    for mid, created, data in conn.execute(
            'SELECT message_id, time_created, data FROM "part" WHERE session_id = ? '
            'ORDER BY message_id, id', (sid,)):
        parts.setdefault(mid, []).append((created, data))
    for mid, created, raw in messages:
        state.row += 1
        data = _json(raw)
        if data is None:
            state.session.unrecognised += 1
            continue
        state.at(created)
        role = data.get("role")
        path = data.get("path")
        if isinstance(path, dict) and clean_path(path.get("cwd")):
            state.cwd = clean_path(path.get("cwd"))
        if role not in ("user", "assistant"):
            state.session.unrecognised += 1
            continue
        prompted = False
        for pcreated, praw in parts.get(mid, []):
            state.row += 1
            state.at(pcreated)
            part = _json(praw)
            ptype = part.get("type") if part else None
            if ptype not in _V1_PARTS:
                state.session.unrecognised += 1
                continue
            if ptype == "text":
                if part.get("synthetic") or part.get("ignored"):
                    continue
                if role == "assistant":
                    state.text(_str(part.get("text")))
                elif not prompted and _str(part.get("text")).strip():
                    state.prompt()
                    prompted = True
            elif ptype == "tool" and role == "assistant":
                tool_state = part.get("state")
                state.tool(_str(part.get("tool")), _str(part.get("callID")),
                           tool_state if isinstance(tool_state, dict) else {})


def _read_v2(conn, state: _Parse, sid: str) -> None:
    messages = conn.execute(
        'SELECT id, type, time_created, data FROM "session_message" WHERE session_id = ? '
        'ORDER BY seq', (sid,)).fetchall()
    parts: dict[str, list] = {}
    for mid, ptype, call_id, data in conn.execute(
            'SELECT message_id, type, call_id, data FROM "session_part" WHERE session_id = ? '
            'ORDER BY message_id, ordinal', (sid,)):
        parts.setdefault(mid, []).append((ptype, call_id, data))
    for mid, mtype, created, raw in messages:
        state.row += 1
        data = _json(raw)
        if data is None or mtype not in _V2_MESSAGES:
            state.session.unrecognised += 1
            continue
        state.at(created)
        if mtype == "user":
            if _str(data.get("text")).strip():
                state.prompt()
        elif mtype == "shell":
            state.user_shell(data)
        elif mtype == "assistant":
            rows = parts.get(mid)
            if rows is None:  # content kept in the message itself
                content = data.get("content")
                rows = [(c.get("type"), None, c) for c in content if isinstance(c, dict)] \
                    if isinstance(content, list) else []
            for ptype, call_id, praw in rows:
                state.row += 1
                part = praw if isinstance(praw, dict) else _json(praw)
                ptype = (part or {}).get("type", ptype)
                if part is None or ptype not in _V2_PARTS:
                    state.session.unrecognised += 1
                    continue
                if ptype == "text":
                    state.text(_str(part.get("text")))
                elif ptype == "tool":
                    tool_state = part.get("state")
                    state.tool(_str(part.get("name")), _str(part.get("id")) or _str(call_id),
                               tool_state if isinstance(tool_state, dict) else {})


def parse_session(path: SessionPath) -> Session:
    db = Path(os.fspath(path))
    sid, schema = path.session_id, path.schema
    state = _Parse(path.label(), sid)
    try:
        with _connect(db) as conn:
            table = _TABLES[schema][0]
            row = conn.execute(f'SELECT directory, version FROM "{table}" WHERE id = ?',
                               (sid,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"no opencode session {sid!r} in {db}")
            state.session.cwd = clean_path(row[0])
            state.cwd = state.session.cwd
            state.session.version = _str(row[1])[:64]
            (_read_v2 if schema == _V2 else _read_v1)(conn, state, sid)
    except sqlite3.Error as exc:
        raise UnreadableSession(_why(exc)) from None
    return state.session


class OpencodeReader:
    name = "opencode"

    @property
    def location(self) -> str:
        return "$OPENCODE_DB" if os.environ.get("OPENCODE_DB") else "~/.local/share/opencode/opencode.db"

    def _list(self, db: Path, project_filter: str | None = None) -> list[SessionPath]:
        if not db.is_file() or not is_database(db):
            return []
        try:
            with _connect(db) as conn:
                rows = _sessions(conn)
        except (sqlite3.Error, UnreadableSession):
            return []
        if project_filter:
            wanted = project_filter.lower()
            rows = [r for r in rows if wanted in clean_path(r[3]).lower()]
        return [session_path(db, sid, schema) for schema, sid, _, _ in rows]

    def discover(self, project_filter: str | None = None) -> list[Path]:
        db = database_path()
        return list(self._list(db, project_filter)) if db is not None else []

    def resolve(self, target: str, project_filter: str | None = None) -> Path:
        if isinstance(target, SessionPath):
            return target
        db_part, sep, wanted = str(target).rpartition("#")
        if sep and db_part and Path(db_part).is_file():
            return self._pick(self._list(Path(db_part), project_filter), wanted or "latest")
        direct = Path(target)
        if direct.is_file():
            if not is_database(direct):
                raise FileNotFoundError(f"{target} is not an opencode database")
            return self._pick(self._list(direct, project_filter), "latest")
        return self._pick(self.discover(project_filter), target)

    def _pick(self, sessions: list, target: str) -> Path:
        if target == "latest":
            if not sessions:
                raise FileNotFoundError(f"no transcripts found under {self.location}")
            return sessions[0]
        for candidate in sessions:
            if candidate.session_id.startswith(target):
                return candidate
        raise FileNotFoundError(f"no transcript matching {target!r}")

    def detect(self, path: Path) -> bool:
        path = Path(path)
        if not is_database(path):
            return False
        try:
            with _connect(path) as conn:
                return bool(_schemas(conn))
        except (sqlite3.Error, UnreadableSession):
            return False

    def describe(self, path: Path) -> tuple[str, str]:
        sid = getattr(path, "session_id", "")
        project = "?"
        try:
            with _connect(Path(os.fspath(path))) as conn:
                row = conn.execute(f'SELECT directory FROM "{_TABLES[path.schema][0]}" '
                                   'WHERE id = ?', (sid,)).fetchone()
            cwd = clean_path(row[0]) if row else ""
            if cwd:
                project = re.split(r"[\\/]", cwd.rstrip("\\/"))[-1] or "?"
        except (sqlite3.Error, UnreadableSession, KeyError, AttributeError):
            pass
        return _UNSAFE.sub("?", sid[:12]), _UNSAFE.sub("?", project)[:60]

    def parse(self, path: Path) -> Session:
        if not isinstance(path, SessionPath):
            path = self.resolve(str(path))
        return parse_session(path)

    def attach_sources(self, session: Session) -> Session:
        session.main_only = False  # the scope line then says no sub-agent sessions were read
        return session  # sub-agent sessions (parent_id) are not read yet


OPENCODE = OpencodeReader()
