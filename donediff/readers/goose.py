"""The Goose reader: sessions in Goose's SQLite database (sessions.db).

Format notes (read from github.com/aaif-goose/goose, Apache-2.0, branch main,
commit d8de0e789f19; the format is internal and moves):

- Where it lives: <data>/sessions/sessions.db (session_manager.rs L33-35,
  L1105, L1116). <data> is $GOOSE_PATH_ROOT/data when GOOSE_PATH_ROOT is an
  absolute path (config/paths.rs, path_root), else the etcetera app folder for
  "Block"/"goose": %APPDATA%\\Block\\goose\\data on Windows, and
  $XDG_DATA_HOME/goose or ~/.local/share/goose elsewhere
  (documentation/docs/guides/logs.md).
- Schema version 18 (CURRENT_SCHEMA_VERSION, session_manager.rs L33; table
  schema_version). Only that version is read: any other is refused with a
  clear message, never guessed at. Tables (L1173-1225): "sessions" (id,
  session_type, working_dir, updated_at, ...) and "messages" (id
  AUTOINCREMENT, session_id, role, content_json, created_timestamp in
  seconds, metadata_json). Messages are read in Goose's own order,
  created_timestamp then id (get_conversation, L2180-2190).
- content_json is a list of blocks tagged "type" (goose-provider-types
  conversation/message.rs L318-330): text, image, document, toolRequest,
  toolResponse, toolConfirmationRequest, actionRequired, thinking,
  redactedThinking, systemNotification, error. toolRequest: {id, toolCall:
  {status: success, value: {name, arguments}} or {status: error, error}}.
  toolResponse: {id, toolResult: {status: success, value: CallToolResult
  {content, isError, structuredContent} or a bare content list} or
  {status: error, error}} (conversation/tool_result_serde.rs).
- Tools are named "<extension>__<tool>" (conversation/tool_request.rs). The
  developer extension (agents/platform_extensions/developer/mod.rs) has
  shell (command), write (path, content), edit (path, before, after), tree,
  read_image. Older Goose (goose-mcp developer, before commit ced5c1b108,
  2026-02-26) had shell and text_editor (command view / write file_text /
  str_replace old_str new_str or diff / insert insert_line new_str /
  undo_edit); both are read.

The exit code (developer/shell.rs L191-210, L311-381): the shell result's
structuredContent holds the process "exit_code" for every finished run, 0
included, and leaves it out when the run was killed (timeout, cancel).
isError is true for a non-zero exit, a timeout or an output collection error.
A real exit code is read ONLY from that structuredContent, never from the
text the command printed, and an error outcome (isError, an error result)
always wins over a success-looking field: an exit_code 0 with isError true
is kept as an error with no number. A legacy shell result has no exit code
at all (its exit status was discarded), so it gives a status only.

Rewinds: Goose truncates a conversation by deleting rows (truncate_conversation
L3014-3050, replace_conversation_inner L2323-2330). Deleted rows cannot be
read. Message ids are AUTOINCREMENT and never reused, so ids missing between
two of a session's rows mean rows were deleted there. A passing exit code
logged before such a gap is then kept as a status only: the deleted part may
have held a later edit or failure, so it can never read as proof. A whole
conversation deleted and re-inserted under new ids (SEC-215) leaves no gap
inside the session; ids missing right below it, or another session's older id
logged later than its first message, mark it, and then no pass in it counts.

The database is opened read-only (URI mode=ro, query_only; immutable for a
WAL file with no side files, SEC-204; a private temp copy when both side files
are there, SEC-218) for one read and closed; nothing is written beside it. A row whose JSON does not parse, an over-size row or metadata, or a
block type this reader does not know is skipped and counted in
Session.unrecognised. Rows are read one at a time under a total row and size
bound; rows past it are counted too, and no pass logged before them is proof.
"""

from __future__ import annotations

import os
import re
import sqlite3
from bisect import bisect_left, bisect_right
import stat as stat_mod
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..evidence import FAILURE_IN_OUTPUT, PASS_IN_OUTPUT, check_types, passed_counts
from ..models import EDIT, OTHER, SHELL, WRITE, Event, EventKind, Session
from ..parser import kept_input, loads
from ..shell import is_remote_path
from .base import UnreadableSession, _absolute, _is_link, _str, pick_session
from .base import _int as _exit
from .codex import clean_path
from .opencode import connect_read_only, is_database, open_mode, refuse_computed

SCHEMA_VERSION = 18  # the one tested version (session_manager.rs L33 at d8de0e789f19)
_COPY_FAILED = "Goose database could not be copied for a read-only look; try again"
_MAX_ROW = 16 * 1024 * 1024  # bytes of content_json read for one message
_MAX_ROWS = 500_000  # messages read for one session
_MAX_META = 64 * 1024  # bytes of metadata_json read for one message (SEC-217)
_MAX_TOTAL = 128 * 1024 * 1024  # content + metadata read for one session (SEC-217)
_MS = 10_000_000_000  # created_timestamp above this is milliseconds (L36, L860)
_DEVELOPER = "developer"
_KNOWN_BLOCKS = frozenset({"text", "image", "document", "toolRequest", "toolResponse",
                           "toolConfirmationRequest", "actionRequired", "thinking",
                           "redactedThinking", "systemNotification", "error"})
_HIDDEN_TYPES = frozenset({"sub_agent", "hidden"})  # not offered as "latest" or in list
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩]")
_ID = re.compile(r"^[0-9A-Za-z_.-]{1,128}$")
_REWOUND = "completed, later log rows were deleted"
# What Goose answers, as a success, for a tool call it skipped in chat mode:
# the tool never ran (block/goose d8de0e7 crates/goose/src/agents/
# state_machine/ops_remaining_tool_calls.rs:87-92, CHAT_MODE_TOOL_SKIPPED_RESPONSE).
_SKIPPED = "Let the user know the tool call was skipped in goose chat mode"
# Goose's own isError text for a call that never ran (block/goose 8ba5f35a1214):
# a permission denied by rule (crates/goose/src/agents/tool_execution.rs
# DECLINED_RESPONSE, used by state_machine/tool_approval.rs:80), a user who
# answered no (crates/goose-agent/src/tool_approval.rs DEFAULT_DECLINED_MESSAGE),
# both written by tool_call_state.rs add_error_response as a success-status
# CallToolResult::error; and a call no operation handled
# (unhandled_tool_calls.rs NOT_HANDLED_MESSAGE). Status words are in
# verify.NO_EVIDENCE_STATUS: a command reads UNVERIFIED (D087).
_NOT_RUN = (("The user has declined to run this tool.", "declined"),
            ("The user declined to run this tool.", "declined"),
            ("This tool call was not handled.", "skipped"))


def _not_run(text: str) -> str | None:
    head = text.lstrip()
    return next((status for start, status in _NOT_RUN if head.startswith(start)), None)


# -- where the database is ----------------------------------------------------

def data_dir() -> Path | None:
    """Goose's data folder, or None when it would be on a remote share.

    A relative or drive-relative APPDATA / XDG_DATA_HOME is ignored, and a
    result that is still not absolute (a relative home) is refused, so it
    never resolves against the working folder (SEC-216).
    """
    root = os.environ.get("GOOSE_PATH_ROOT")
    if _absolute(root) is not None:
        result = Path(root) / "data"
    elif sys.platform == "win32":
        appdata = _absolute(os.environ.get("APPDATA"))
        base = appdata or Path(os.path.expanduser("~")) / "AppData" / "Roaming"
        result = base / "Block" / "goose" / "data"
    else:
        xdg = _absolute(os.environ.get("XDG_DATA_HOME"))
        result = (xdg or Path(os.path.expanduser("~")) / ".local" / "share") / "goose"
    if is_remote_path(os.fspath(result)) or not result.is_absolute():
        return None
    return result


def database_path() -> Path | None:
    data = data_dir()
    if data is None:
        return None
    result = data / "sessions" / "sessions.db"
    return None if is_remote_path(os.fspath(result)) else result


def _home_form(path: Path) -> str:
    """The path with the home folder shown as ~ (as every reader names its location)."""
    try:
        rest = Path(path).relative_to(Path(os.path.expanduser("~")))
    except ValueError:
        return os.fspath(path)
    return "~/" + rest.as_posix()


def _usable(db: Path) -> bool:
    return (not is_remote_path(os.fspath(db)) and not _is_link(db) and not _is_link(db.parent)
            and db.is_file() and is_database(db))


# -- opening it read-only -------------------------------------------------------

def _open_mode(db: Path) -> str:
    """URI query that opens the file without creating anything next to it (SEC-204).

    Goose runs its database in WAL mode (session_manager.rs L1099). With no
    -shm/-wal beside it, open immutable (nothing created, nothing locked; a
    Goose that starts writing mid-read is the trade-off). With both, Goose
    owns them, and even mode=ro rewrites the -shm in place, so COPY: the read
    goes to a private temp copy (SEC-218). A lone -wal is refused: an immutable
    read would miss its writes, and a missed failure must never become a pass.
    """
    return open_mode(db, "Goose")


def _why(exc: sqlite3.Error) -> str:
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "Goose database is locked by another program; try again when it is idle"
    return "Goose database could not be read (damaged, or not a Goose database)"


class _Conn:
    """A read-only connection, closed on exit."""

    def __init__(self, db: Path):
        self.db = db
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        try:
            self.conn = connect_read_only(self.db, _open_mode(self.db), _COPY_FAILED)
            refuse_computed(self.conn, ("schema_version", "sessions", "messages"), "Goose")
        except (sqlite3.Error, UnreadableSession) as exc:
            if self.conn is not None:
                self.conn.close()
            if isinstance(exc, UnreadableSession):
                raise
            raise UnreadableSession(_why(exc)) from None
        return self.conn

    def __exit__(self, *exc) -> None:
        if self.conn is not None:
            self.conn.close()


def _connect(db: Path) -> _Conn:
    return _Conn(db)


def _is_goose(conn: sqlite3.Connection) -> bool:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"schema_version", "sessions", "messages"} <= names:
        return False
    columns = {row[1] for row in conn.execute('PRAGMA table_info("messages")')}
    return {"content_json", "created_timestamp", "role", "session_id"} <= columns


def _version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute('SELECT MAX(version) FROM "schema_version"').fetchone()
    value = row[0] if row else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _check_version(conn: sqlite3.Connection) -> int:
    version = _version(conn)
    if version != SCHEMA_VERSION:
        raise UnreadableSession(
            f"Goose database schema version {version if version is not None else 'unknown'} "
            f"is not supported (this donediff reads version {SCHEMA_VERSION}); "
            "nothing was audited")
    return version


def _seconds(value) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return value / 1000 if value > _MS else float(value)


def _parse_time(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _iso(seconds: float) -> str:
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _sessions(conn: sqlite3.Connection) -> list[tuple[str, str, str, float]]:
    """(id, working_dir, session_type, last activity) of every session, newest first."""
    rows = []
    for sid, cwd, stype, updated, last in conn.execute(
            'SELECT s.id, s.working_dir, s.session_type, s.updated_at, '
            f'MAX(CASE WHEN m.created_timestamp > {_MS} THEN m.created_timestamp / 1000 '
            'ELSE m.created_timestamp END) '
            'FROM "sessions" s LEFT JOIN "messages" m ON m.session_id = s.id GROUP BY s.id'):
        if not (isinstance(sid, str) and _ID.match(sid)):
            continue
        when = _seconds(last)
        if when is None:
            when = _parse_time(updated) or 0.0
        rows.append((sid, cwd if isinstance(cwd, str) else "",
                     stype if isinstance(stype, str) else "", when))
    rows.sort(key=lambda r: (r[3], r[0]), reverse=True)
    return rows


class SessionPath(type(Path())):
    """One Goose session: the database file plus the session id.

    It stats as a file whose size is the session's stored messages and whose
    time is its last message, so `donediff list` and the scan size read right.
    """

    session_id: str = ""
    _stat: os.stat_result | None = None

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        if self._stat is None:
            base = super().stat(follow_symlinks=follow_symlinks)
            size, updated = base.st_size, base.st_mtime
            try:
                with _connect(Path(os.fspath(self))) as conn:
                    row = conn.execute(
                        'SELECT COALESCE(SUM(LENGTH(CAST(content_json AS BLOB))), 0), MAX(created_timestamp) '
                        'FROM "messages" WHERE session_id = ?', (self.session_id,)).fetchone()
                    size = int(row[0] or 0)
                    updated = _seconds(row[1]) or updated
            except (sqlite3.Error, UnreadableSession):
                pass
            self._stat = os.stat_result((stat_mod.S_IFREG | 0o444, 0, 0, 1, 0, 0, size,
                                         updated, updated, updated))
        return self._stat

    def label(self) -> str:
        return f"{os.fspath(self)}#{self.session_id}"


def session_path(db: Path, session_id: str) -> SessionPath:
    path = SessionPath(db)
    path.session_id = session_id
    return path


# -- what a row holds ---------------------------------------------------------

def _json(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    try:
        return loads(raw)  # too deep: ValueError (SEC-207)
    except ValueError:
        return None


def _content_text(content) -> str:
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        if isinstance(block, dict):
            if isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif isinstance(block.get("resource"), dict) and isinstance(block["resource"].get("text"), str):
                texts.append(block["resource"]["text"])
    return "\n".join(texts)


def _tool_name(request: dict, name: str) -> tuple[str | None, str]:
    """(extension, tool) the way Goose splits a name (tool_request.rs L6-38)."""
    meta = request.get("_meta")
    extension = meta.get("goose_extension") if isinstance(meta, dict) else None
    if isinstance(extension, str) and extension:
        rest = name[len(extension):] if name.startswith(extension) else ""
        return extension, rest[2:] if rest.startswith("__") else name
    ext, sep, tool = name.partition("__")
    return (ext, tool) if sep else (None, name)


def _diff_sides(diff: str) -> tuple[str, str]:
    """The old and new text of a unified diff (legacy text_editor str_replace diff)."""
    old, new = [], []
    for line in diff.splitlines():
        if line.startswith(("---", "+++", "@@", "\\")):
            continue
        if line.startswith("-"):
            old.append(line[1:])
        elif line.startswith("+"):
            new.append(line[1:])
        else:
            text = line[1:] if line.startswith(" ") else line
            old.append(text)
            new.append(text)
    return "\n".join(old), "\n".join(new)


# -- the reader ---------------------------------------------------------------

class _Parse:
    """One read of one session, building the Session."""

    def __init__(self, label: str, sid: str, cwd: str):
        self.session = Session(path=label, session_id=sid, slug=sid, cwd=cwd)
        self.index = 0
        self.row = 0
        self.clock = float("-inf")
        self.timestamp = ""
        self.pending: dict[str, list[Event]] = {}
        self.row_of: dict[int, int] = {}  # id(event) -> message row id

    def at(self, seconds: float | None) -> None:
        if seconds is None:
            return
        stamp = _iso(seconds)
        if not stamp:
            return
        self.timestamp = stamp
        self.session.first_timestamp = self.session.first_timestamp or stamp
        self.session.last_timestamp = stamp
        self.clock = max(self.clock, seconds)

    def text(self, text: str) -> None:
        text = text.strip()
        if text:
            self.session.events.append(Event(kind=EventKind.TEXT, index=self.index,
                                             timestamp=self.timestamp, text=text, t=self.clock))
            self.index += 1

    def prompt(self) -> None:
        self.session.prompt_starts.append(self.index)
        self.session.prompt_marks.append((self.index, self.row))

    def _event(self, name: str, action: str, call_id: str, tool_input: dict, rowid: int) -> Event:
        event = Event(kind=EventKind.TOOL_CALL, index=self.index, timestamp=self.timestamp,
                      tool_name=name, action=action, tool_id=call_id, tool_input=tool_input,
                      t=self.clock, cwd=self.session.cwd, result_seen=False)
        if action == SHELL:
            event.check_types = check_types(event.command)
        self.session.events.append(event)
        self.index += 1
        self.row_of[id(event)] = rowid
        return event

    def request(self, block: dict, rowid: int) -> None:
        call = block.get("toolCall")
        call_id = _str(block.get("id"))
        if not isinstance(call, dict) or call.get("status") != "success" \
                or not isinstance(call.get("value"), dict):
            return  # a tool call the model got wrong: Goose never ran it
        value = call["value"]
        name = _str(value.get("name"))
        args = value.get("arguments")
        args = args if isinstance(args, dict) else {}
        extension, tool = _tool_name(block, name)
        # Only a name the developer extension owns: "developer__<tool>", or
        # Goose's own _meta goose_extension. A bare "shell" is any tool (SEC-219).
        developer = extension == _DEVELOPER
        path = clean_path(args.get("path"))
        if developer and tool == "shell":
            event = self._event(name, SHELL, call_id, {"command": _str(args.get("command"))}, rowid)
        elif developer and tool == "write":
            event = self._event(name, WRITE, call_id,
                                {"file_path": path, "content": _str(args.get("content"))}, rowid)
        elif developer and tool == "edit":
            event = self._event(name, EDIT, call_id, {
                "file_path": path, "old_string": _str(args.get("before")),
                "new_string": _str(args.get("after"))}, rowid)
        elif developer and tool == "text_editor":
            event = self._text_editor(name, call_id, args, path, rowid)
        else:
            event = self._event(name, OTHER, call_id, kept_input(args, edits=False), rowid)  # NEW2-I-4
        self.pending.setdefault(call_id, []).append(event)

    def _text_editor(self, name: str, call_id: str, args: dict, path: str, rowid: int) -> Event:
        command = _str(args.get("command"))
        if command == "write":
            return self._event(name, WRITE, call_id,
                               {"file_path": path, "content": _str(args.get("file_text"))}, rowid)
        if command == "str_replace":
            if isinstance(args.get("diff"), str):
                old, new = _diff_sides(args["diff"])
            else:
                old, new = _str(args.get("old_str")), _str(args.get("new_str"))
            return self._event(name, EDIT, call_id,
                               {"file_path": path, "old_string": old, "new_string": new}, rowid)
        if command in ("insert", "undo_edit"):  # both change the file
            return self._event(name, EDIT, call_id, {
                "file_path": path, "old_string": "", "new_string": _str(args.get("new_str"))}, rowid)
        return self._event(name, OTHER, call_id, kept_input(args, edits=False), rowid)  # NEW2-I-4

    @staticmethod
    def _hints(event: Event) -> None:
        event.failure_hint = bool(FAILURE_IN_OUTPUT.search(event.output))
        event.pass_hint = bool(PASS_IN_OUTPUT.search(event.output))
        event.passed_counts = passed_counts(event.output) if event.check_types else frozenset()

    def response(self, block: dict) -> None:
        call_id = _str(block.get("id"))
        events = self.pending.get(call_id)
        result = block.get("toolResult")
        if not events or not isinstance(result, dict):
            return
        failed = result.get("status") != "success"
        value = result.get("value")
        if failed:
            text, is_error, structured = _str(result.get("error")), True, None
        elif isinstance(value, list):  # an older bare content list: no isError, no exit code
            text, is_error, structured = _content_text(value), None, None
        elif isinstance(value, dict):
            text = _content_text(value.get("content"))
            is_error = value.get("isError") if isinstance(value.get("isError"), bool) else None
            structured = value.get("structuredContent")
            structured = structured if isinstance(structured, dict) else None
        else:
            return
        code = _exit(structured.get("exit_code")) if structured else None
        # A command's own output can start with Goose's skip or decline text
        # (SEC-234): it reads "did not run" only with no error flag or a zero
        # exit code for the skip, and never when the text reports failures.
        ran_and_failed = bool(FAILURE_IN_OUTPUT.search(text))
        if (not failed and is_error is not True and code in (None, 0)
                and not ran_and_failed and text.lstrip().startswith(_SKIPPED)):
            # Skipped in chat mode: never ran, so no evidence of anything (SEC-214).
            # A write or edit is a failure; a command is status only (it did
            # not fail either), with no exit code and no hints from the text.
            for event in events:
                event.mark_not_run("declined", text)
            return
        not_run = _not_run(text) if is_error and not failed and not ran_and_failed else None
        if not_run is not None and (structured is None or "exit_code" not in structured):
            for event in events:
                if not (event.result_seen and event.is_error):  # a logged failure still wins
                    event.mark_not_run(not_run, text)  # never ran: no evidence either way
            return
        for event in events:
            if event.result_seen and not (is_error and not event.is_error):
                continue  # a second response only counts when it reports a failure
            event.result_seen = True
            event.output = text
            event.exit_code = None
            if event.tool_action != SHELL:
                event.is_error = bool(is_error)
                event.status = "error" if is_error else "completed"
                continue
            self._shell_outcome(event, failed, is_error, structured)
            self._hints(event)

    @staticmethod
    def _shell_outcome(event: Event, failed: bool, is_error: bool | None,
                       structured: dict | None) -> None:
        code = _exit(structured.get("exit_code")) if structured else None
        timed_out = bool(structured and structured.get("timed_out") is True)
        if structured and structured.get("output_truncated") is True:
            event.output_truncated = True
        if failed:
            event.is_error, event.status = True, "error"
        elif timed_out:
            event.is_error, event.status = True, "timeout"
        elif code is not None and code != 0:
            event.exit_code, event.is_error, event.status = code, True, "failed"
        elif is_error:
            # an error outcome wins over a success-looking exit_code 0
            event.is_error, event.status = True, "error"
        elif code == 0 and is_error is False:
            event.exit_code, event.is_error, event.status = 0, False, "completed"
        else:
            event.is_error, event.status = False, "completed"  # a status only

    def demote_before(self, rowid: int) -> None:
        """Passing exit codes logged before deleted rows become a status only."""
        for event in self.session.events:
            if (event.kind is EventKind.TOOL_CALL and event.exit_code == 0
                    and self.row_of.get(id(event), rowid + 1) <= rowid):
                event.exit_code = None
                event.status = _REWOUND


_SECONDS_SQL = f"CASE WHEN created_timestamp > {_MS} THEN created_timestamp / 1000 ELSE created_timestamp END"


def _rewritten(conn, sid: str, low: int) -> bool:
    """True when the session's rows look deleted and re-inserted (SEC-215).

    replace_conversation_inner (session_manager.rs L2323-2330, used by
    session --edit / --fork and the state machine) deletes every row of the
    session and inserts the kept part again under new ids. Two traces of that:
    ids missing right below the session's lowest id, or an older id (another
    session's row) logged later than this session's first message, which a
    plain append can never produce.
    """
    prev = conn.execute('SELECT MAX(id) FROM "messages" WHERE id < ?', (low,)).fetchone()[0]
    if low - (prev if isinstance(prev, int) else 0) > 1:
        return True
    first = conn.execute(f'SELECT MIN({_SECONDS_SQL}) FROM "messages" WHERE session_id = ?',
                         (sid,)).fetchone()[0]
    if not isinstance(first, (int, float)) or isinstance(first, bool):
        return True  # no usable time: the order cannot be proved
    later = conn.execute(f'SELECT 1 FROM "messages" WHERE id < ? AND {_SECONDS_SQL} > ? LIMIT 1',
                         (low, first)).fetchone()
    return later is not None


def _last_deleted_gap(conn, ids: list[int], sid: str = "") -> int | None:
    """The last of this session's row ids that has deleted rows right after it.

    A whole-conversation rewrite returns the highest id: nothing in the
    session can then prove a pass (SEC-215).
    """
    if not ids:
        return None
    low, high = ids[0], ids[-1]
    if _rewritten(conn, sid, low):
        return high
    if len(ids) < 2:
        return None
    total = conn.execute('SELECT COUNT(*) FROM "messages" WHERE id BETWEEN ? AND ?',
                         (low, high)).fetchone()[0]
    if total == high - low + 1:
        return None
    # One ordered pass over every id in the range, then a binary search per
    # gap: a COUNT per gap is a full scan each when "id" is not the rowid,
    # quadratic on a crafted database (SEC-242).
    present = [row[0] for row in conn.execute(
        'SELECT id FROM "messages" WHERE id BETWEEN ? AND ? ORDER BY id', (low, high))]
    for before, after in zip(reversed(ids[:-1]), reversed(ids[1:])):
        if after - before > 1:
            between = bisect_left(present, after) - bisect_right(present, before)
            if between < after - before - 1:
                return before
    return None


def _visible_to_user(metadata) -> bool:
    data = _json(metadata) if metadata is not None else None
    return not (isinstance(data, dict) and data.get("userVisible") is False)


def parse_session(path: SessionPath) -> Session:
    db = Path(os.fspath(path))
    sid = path.session_id
    try:
        with _connect(db) as conn:
            if not _is_goose(conn):
                raise UnreadableSession("not a Goose database")
            version = _check_version(conn)
            row = conn.execute('SELECT working_dir FROM "sessions" WHERE id = ?', (sid,)).fetchone()
            if row is None:
                raise FileNotFoundError(f'no Goose session "{sid}" in {db}')
            state = _Parse(path.label(), sid, clean_path(row[0]))
            state.session.version = f"schema {version}"
            stored = conn.execute('SELECT COUNT(*) FROM "messages" WHERE session_id = ?',
                                  (sid,)).fetchone()[0]
            ids, read, budget, cut = [], 0, _MAX_TOTAL, stored > _MAX_ROWS
            # One row in memory at a time (SEC-217): content and metadata are
            # capped per row, and all rows together by _MAX_TOTAL. Lengths are
            # in bytes: LENGTH() of text stops at a NUL (SEC-230).
            for rowid, role, created, metadata, meta_over, raw in conn.execute(
                    'SELECT id, role, created_timestamp, '
                    'CASE WHEN LENGTH(CAST(metadata_json AS BLOB)) > ? THEN NULL ELSE metadata_json END, '
                    'COALESCE(LENGTH(CAST(metadata_json AS BLOB)) > ?, 0), '
                    'CASE WHEN LENGTH(CAST(content_json AS BLOB)) > ? THEN NULL ELSE content_json END '
                    'FROM "messages" WHERE session_id = ? ORDER BY created_timestamp, id LIMIT ?',
                    (_MAX_META, _MAX_META, _MAX_ROW, sid, _MAX_ROWS)):
                size = _size(raw) + _size(metadata)
                if size > budget:
                    cut = True
                    break
                budget -= size
                read += 1
                if isinstance(rowid, int):
                    ids.append(rowid)
                if meta_over:
                    state.session.unrecognised += 1  # unread metadata hides nothing
                _row(state, rowid, role, created, metadata, raw)
            ids.sort()
            gap = _last_deleted_gap(conn, ids, sid)
    except sqlite3.Error as exc:
        raise UnreadableSession(_why(exc)) from None
    if cut:
        # Rows past the bound are not read: counted, and since one of them may
        # hold a later edit or failure, no pass before them is proof.
        state.session.unrecognised += max(stored - read, 1)
        gap = ids[-1] if ids else gap
    if gap is not None:
        state.demote_before(gap)
    return state.session


def _size(value) -> int:
    return len(value) if isinstance(value, (str, bytes)) else 0


def _row(state: _Parse, rowid, role, created, metadata, raw) -> None:
    state.row += 1
    content = _json(raw)
    if role not in ("user", "assistant") or not isinstance(content, list):
        state.session.unrecognised += 1
        return
    state.at(_seconds(created))
    prompted = False
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in _KNOWN_BLOCKS:
            state.session.unrecognised += 1
            continue
        if kind == "text":
            text = _str(block.get("text"))
            if role == "assistant":
                state.text(text)
            elif not prompted and text.strip() and _visible_to_user(metadata):
                state.prompt()
                prompted = True
        elif kind == "toolRequest" and role == "assistant":
            state.request(block, rowid)
        elif kind == "toolResponse":
            state.response(block)


class GooseReader:
    name = "goose"
    label = "Goose"  # the agent as messages name it
    id_width = 12  # `list` shows at least this much of a session id

    @property
    def location(self) -> str:
        db = database_path()
        return _home_form(db) if db is not None else "the Goose sessions database"

    def _list(self, db: Path, project_filter: str | None = None, strict: bool = False,
              hidden: bool = False) -> list[SessionPath]:
        if not _usable(db):
            return []
        try:
            with _connect(db) as conn:
                if not _is_goose(conn):
                    return []
                _check_version(conn)
                rows = _sessions(conn)
        except UnreadableSession:
            if strict:
                raise
            return []
        except sqlite3.Error as exc:
            if strict:
                raise UnreadableSession(_why(exc)) from None
            return []
        if not hidden:
            rows = [r for r in rows if r[2] not in _HIDDEN_TYPES]
        if project_filter:
            wanted = project_filter.lower()
            rows = [r for r in rows if wanted in clean_path(r[1]).lower()]
        return [session_path(db, r[0]) for r in rows]

    def discover(self, project_filter: str | None = None) -> list[Path]:
        db = database_path()
        return list(self._list(db, project_filter)) if db is not None else []

    def resolve(self, target: str, project_filter: str | None = None) -> Path:
        if isinstance(target, SessionPath):
            return target
        text = str(target)
        if is_remote_path(text):
            raise FileNotFoundError(f"{text} is on a remote share; donediff does not read it")
        db_part, sep, wanted = text.rpartition("#")
        if sep and db_part and Path(db_part).is_file():
            return self._pick(self._direct(Path(db_part), project_filter, True), wanted or "latest")
        direct = Path(text)
        if direct.is_file():
            return self._pick(self._direct(direct, project_filter, False), "latest")
        db = database_path()
        if db is None or not _usable(db):
            raise FileNotFoundError(f"no transcripts found under {self.location}")
        if text == "latest":
            return self._pick(self._list(db, project_filter, strict=True), "latest")
        return self._pick(self._list(db, project_filter, strict=True, hidden=True), text)

    def _direct(self, db: Path, project_filter, hidden: bool) -> list[SessionPath]:
        if _is_link(db) or not is_database(db):
            raise FileNotFoundError(f"{db} is not a Goose database")
        return self._list(db, project_filter, strict=True, hidden=hidden)

    def _pick(self, sessions: list, target: str) -> Path:
        return pick_session(sessions, target, self.session_keys, self.location)

    def session_keys(self, path: Path) -> tuple[str, ...]:
        return (getattr(path, "session_id", ""),)

    def detect(self, path: Path) -> bool:
        path = Path(path)
        if is_remote_path(os.fspath(path)) or _is_link(path) or not is_database(path):
            return False
        try:
            with _connect(path) as conn:
                return _is_goose(conn)
        except (sqlite3.Error, UnreadableSession):
            return False

    def describe(self, path: Path) -> tuple[str, str]:
        sid = getattr(path, "session_id", "")
        project = "?"
        try:
            with _connect(Path(os.fspath(path))) as conn:
                row = conn.execute('SELECT working_dir FROM "sessions" WHERE id = ?',
                                   (sid,)).fetchone()
            cwd = clean_path(row[0]) if row else ""
            if cwd:
                project = re.split(r"[\\/]", cwd.rstrip("\\/"))[-1] or "?"
        except (sqlite3.Error, UnreadableSession):
            pass
        return _UNSAFE.sub("?", sid[:12]), _UNSAFE.sub("?", project)[:60]

    def parse(self, path: Path) -> Session:
        if not isinstance(path, SessionPath):
            path = self.resolve(str(path))
        return parse_session(path)

    def attach_sources(self, session: Session) -> Session:
        # sub-agent sessions (parent_session_id) are not read yet
        return session


GOOSE = GooseReader()
