"""The Gemini CLI reader: chat files under ~/.gemini/tmp/<project>/chats.

Format notes (read from github.com/google-gemini/gemini-cli, Apache-2.0,
branch main at commit 4ed06ed0ddc5; the format is internal and moves):

- Home: $GEMINI_CLI_HOME when set, else the user's home
  (packages/core/src/utils/paths.ts homedir()), then .gemini; under
  SANDBOX=sandbox-exec the runtime folder is <home>/.cache/.gemini
  (packages/core/src/config/storage.ts getGlobalRuntimeDir()).
- Sessions: <runtime>/tmp/<project id>/chats/session-<YYYY-MM-DDTHH-MM>-
  [<n>-]<first 8 of the session id>.jsonl (chatRecordingService.ts). The
  project id is a slug of the project folder's name, with a ".project_root"
  file beside "chats" holding the normalised project path (projectRegistry.ts);
  older builds used the sha256 of the project path as the folder name, with no
  such file. Builds before JSONL wrote one session-*.json document per session.
  Sub-agent chats sit in chats/<parent session id>/ and are not read yet.
- Lines (chatRecordingTypes.ts): the first is the metadata record
  {sessionId, projectHash, startTime, lastUpdated, kind?, directories?}; then
  message records {id, timestamp, type: user|gemini|info|error|warning,
  content: a string, a Part or a list of Parts}; a "gemini" message carries
  toolCalls [{id, name, args, result?: Parts, status, timestamp}]. The same
  message id is appended again each time it changes (a tool call is first
  written "scheduled", then again with its result): the last copy wins and the
  message keeps its first place. {"$rewindTo": id} drops that message and all
  after it; {"$patch": {id?, content?, toolCalls?, updates?, removeIds?,
  orderIds?}} patches, removes or reorders; {"$set": {...}} updates metadata
  ("messages" there replaces the whole history). This reader replays them the
  way Gemini's own loader does (createJsonlRecordAccumulator).
- Tool call status (scheduler/types.ts): validating, scheduled, executing,
  awaiting_approval (no result yet), success, error, cancelled. A result is a
  list of Parts whose functionResponse.response holds {output} or {error}.
- run_shell_command (tools/shell.ts): args command, dir_path. Its output,
  wrapped in <untrusted_context>, reads "Output: ...", then optional lines
  "Error: ...", "Exit Code: N", "Signal: ...", "Background PIDs: ...",
  "Process Group PGID: N". "Exit Code" is written only when the code is not
  0, and a non-zero exit still has status "success". So a failed run reads
  its exit code here, and a passing run has a status only: never an exit 0.
- write_file (file_path, content) is a write, replace (file_path, old_string,
  new_string) an edit; every other tool is kept under its own name.
- Retention: Gemini deletes sessions older than 30 days by default.

Lines are read in binary, one at a time, capped at MAX_LINE bytes; a longer
line, a line that is not JSON, a record this reader does not know and a
message type it does not know are skipped and counted in Session.unrecognised.
The reader starts no process and makes no network call; paths it reads from
the log are checked later through donediff.shell.confined like every other
transcript path.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from ..evidence import FAILURE_IN_OUTPUT, PASS_IN_OUTPUT, check_types, log_int, passed_counts
from ..models import EDIT, OTHER, SHELL, WRITE, Event, EventKind, Session
from ..parser import epoch, loads
from ..shell import is_remote_path
from .base import UnreadableSession, _str, pick_session
from .codex import _UNSAFE, _join, _lines, clean_path

MAX_LINE = 32 * 1024 * 1024  # bytes in one log line; a longer line is skipped
MAX_JSON = 64 * 1024 * 1024  # bytes of an older one-document .json session
_META_LIMIT = 4 * 1024 * 1024  # bytes of the first line read for discovery and detect
_ROOT_LIMIT = 4096  # bytes of a .project_root file

_FILE = re.compile(r"^session-[0-9A-Za-z_.-]{1,200}\.jsonl?$")
_PROJECT = re.compile(r"^[0-9A-Za-z_.-]{1,200}$")
_TYPES = frozenset({"user", "gemini", "info", "error", "warning"})
_WAITING = frozenset({"validating", "scheduled", "executing", "awaiting_approval"})
_ENDED = frozenset({"success", "error", "cancelled"})
_SHELL_TOOLS = frozenset({"run_shell_command"})
_WRITE_TOOLS = frozenset({"write_file"})
_EDIT_TOOLS = frozenset({"replace"})
_WRAPPED = re.compile(r"\A\s*<untrusted_context[^>\n]*>\n(.*)\n</untrusted_context>\s*\Z", re.S)
# The trailer lines shell.ts adds after "Output: ...", last first.
_TRAILER = (
    re.compile(r"^Process Group PGID: \d+$"),
    re.compile(r"^Background PIDs: [\d, ]+$"),
    re.compile(r"^Signal: \S+$"),
    re.compile(r"^Exit Code: (-?\d+)$"),
)
_BACKGROUND = ("Command moved to background (PID:", "Command is running in background. PID:")


# -- where the sessions are ---------------------------------------------------

def gemini_dir() -> Path | None:
    """The runtime .gemini folder, or None when $GEMINI_CLI_HOME is remote or missing."""
    env = os.environ.get("GEMINI_CLI_HOME")
    if env:
        home = Path(os.path.abspath(env))  # a relative value joins the current folder
        if is_remote_path(env) or is_remote_path(home) or not home.is_dir():
            return None
    else:
        home = Path(os.path.expanduser("~"))
    if os.environ.get("SANDBOX") == "sandbox-exec":
        result = home / ".cache" / ".gemini"
    else:
        result = home / ".gemini"
    return None if is_remote_path(result) else result


def tmp_dir() -> Path | None:
    root = gemini_dir()
    return root / "tmp" if root is not None else None


def _entries(path: Path):
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _is_plain_dir(entry) -> bool:
    return (entry.is_dir(follow_symlinks=False)
            and not getattr(entry, "is_junction", lambda: False)())


def _sessions_in(chats: Path) -> list[tuple[float, Path]]:
    found = []
    for entry in _entries(chats):
        if _FILE.match(entry.name) and entry.is_file(follow_symlinks=False):
            try:
                found.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
            except OSError:
                continue
    return found


def project_root(path: Path) -> str:
    """The project path from the .project_root file beside a session's chats folder."""
    chats = Path(path).parent
    if chats.name != "chats":
        return ""
    marker = chats.parent / ".project_root"
    if is_remote_path(marker):
        return ""
    try:
        if not stat.S_ISREG(os.lstat(marker).st_mode):
            return ""  # a symlink or anything else is not followed
        with open(marker, "rb") as fh:
            data = fh.read(_ROOT_LIMIT + 1)
    except OSError:
        return ""
    if len(data) > _ROOT_LIMIT:
        return ""
    return clean_path(data.decode("utf-8", errors="replace").strip())


def _stem(name: str) -> str:
    return re.sub(r"\.jsonl?$", "", name)


def short_id(name: str) -> str:
    """The first 8 of the session id in a file name (after session-<16-char time>-[n-])."""
    stem = _stem(name)
    core = stem[len("session-"):] if stem.startswith("session-") else stem
    if len(core) > 17 and core[16] == "-":
        core = core[17:]
        rest = re.sub(r"^\d+-", "", core)
        return rest or core
    return core


def _is_meta(record) -> bool:
    return (isinstance(record, dict) and isinstance(record.get("sessionId"), str)
            and isinstance(record.get("projectHash"), str)
            and "type" not in record and "payload" not in record)


def _read_json(path: Path) -> dict | None:
    """An older one-document .json session, or None (too big, not JSON, wrong shape)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_JSON + 1)
    except OSError:
        return None
    if len(data) > MAX_JSON:
        return None
    try:
        record = loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return record if _is_meta(record) else None


def first_record(path: Path) -> dict | None:
    """The metadata record a session file starts with, or None."""
    path = Path(path)
    if is_remote_path(path):
        return None
    if path.name.endswith(".json"):
        return _read_json(path) if _FILE.match(path.name) else None
    try:
        with open(path, "rb") as fh:
            for line in _lines(fh, _META_LIMIT):
                if line is None:
                    return None
                line = line.strip()
                if not line:
                    continue
                record = loads(line.decode("utf-8", errors="replace"))
                return record if _is_meta(record) else None
    except (OSError, ValueError):
        return None
    return None


# -- what a record holds ------------------------------------------------------

def parts_text(content, skip_thoughts: bool = True) -> str:
    """The text of a PartListUnion: a string, a Part or a list of either."""
    items = content if isinstance(content, list) else [content]
    texts = []
    for item in items:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            if not (skip_thoughts and item.get("thought") is True):
                texts.append(item["text"])
    return "\n".join(t for t in texts if t)


def result_text(result) -> tuple[str, bool]:
    """(text, has_error) of a tool call's result Parts."""
    items = result if isinstance(result, list) else [result]
    texts, has_error = [], False
    for item in items:
        if isinstance(item, str):
            texts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        response = item.get("functionResponse")
        if isinstance(response, dict):
            body = response.get("response")
            if isinstance(body, dict):
                if isinstance(body.get("output"), str):
                    texts.append(body["output"])
                if isinstance(body.get("error"), str):
                    texts.append(body["error"])
                    has_error = True
        elif isinstance(item.get("text"), str):
            texts.append(item["text"])
    return "\n".join(t for t in texts if t), has_error


def unwrap(text: str) -> str:
    match = _WRAPPED.match(text)
    return match.group(1) if match else text


def shell_result(text: str) -> tuple[int | None, str]:
    """(exit code, output body) from run_shell_command's own result format.

    The code is read only from an "Exit Code: N" trailer line after an
    "Output: " body, as shell.ts writes it, and only when N is not 0: the tool
    never writes "Exit Code: 0", so such a line came from the command's output.
    """
    text = unwrap(text)
    if not text.startswith("Output: "):
        return None, text
    lines = text.split("\n")
    code = None
    for pattern in _TRAILER:
        if len(lines) > 1:
            match = pattern.match(lines[-1])
            if match:
                lines.pop()
                if match.groups():
                    code = log_int(match.group(1), 1)
    body = "\n".join(lines)[len("Output: "):]
    if body == "(empty)":
        body = ""
    return (code if code else None), body


# -- the reader ---------------------------------------------------------------

class _History:
    """The session's messages, replayed the way Gemini's own loader does."""

    def __init__(self, session: Session):
        self.session = session
        self.meta: dict = {}
        self.messages: dict[str, dict] = {}  # id -> latest copy, in first-seen order

    def add(self, record) -> None:
        if not isinstance(record, dict):
            self.session.unrecognised += 1
        elif isinstance(record.get("$rewindTo"), str):
            self.rewind(record["$rewindTo"])
        elif isinstance(record.get("$patch"), dict):
            self.patch(record["$patch"])
        elif isinstance(record.get("id"), str) and "$patch" not in record:
            self.message(record)
        elif isinstance(record.get("$set"), dict):
            update = record["$set"]
            if isinstance(update.get("messages"), list):
                self.messages.clear()
                self.many(update["messages"])
            self.meta.update({k: v for k, v in update.items() if k != "messages"})
        elif _is_meta(record):
            self.meta.update({k: v for k, v in record.items() if k != "messages"})
            if isinstance(record.get("messages"), list):
                self.many(record["messages"])
        else:
            self.session.unrecognised += 1

    def many(self, messages: list) -> None:
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("id"), str):
                self.message(msg)
            else:
                self.session.unrecognised += 1

    def message(self, record: dict) -> None:
        if record.get("type") not in _TYPES:
            self.session.unrecognised += 1
            return
        self.messages[record["id"]] = record

    def rewind(self, target: str) -> None:
        ids = list(self.messages)
        if target in self.messages:
            for mid in ids[ids.index(target):]:
                del self.messages[mid]
        else:
            self.messages.clear()

    def _apply(self, patch: dict) -> None:
        msg = self.messages.get(patch.get("id"))
        if msg is None:
            return
        msg = dict(msg)
        if "content" in patch and patch["content"] is not None:
            msg["content"] = patch["content"]
        calls = msg.get("toolCalls")
        if isinstance(patch.get("toolCalls"), list) and msg.get("type") == "gemini" \
                and isinstance(calls, list):
            calls = [dict(c) if isinstance(c, dict) else c for c in calls]
            for tc_patch in patch["toolCalls"]:
                if not isinstance(tc_patch, dict) or "result" not in tc_patch:
                    continue
                for call in calls:
                    if isinstance(call, dict) and call.get("id") == tc_patch.get("id"):
                        call["result"] = tc_patch["result"]
                        break
            msg["toolCalls"] = calls
        self.messages[msg["id"]] = msg

    def patch(self, patch: dict) -> None:
        if isinstance(patch.get("id"), str):
            self._apply(patch)
        for update in patch.get("updates") or []:
            if isinstance(update, dict) and isinstance(update.get("id"), str):
                self._apply(update)
        for mid in patch.get("removeIds") or []:
            if isinstance(mid, str):
                self.messages.pop(mid, None)
        order = patch.get("orderIds")
        if isinstance(order, list):
            ordered = [(i, self.messages[i]) for i in dict.fromkeys(order)
                       if isinstance(i, str) and i in self.messages]
            keep = {i for i, _ in ordered}
            prefix = [(i, m) for i, m in self.messages.items() if i not in keep]
            self.messages = dict(prefix + ordered)


class _Build:
    """Events from the replayed messages, in order."""

    def __init__(self, session: Session, cwd: str):
        self.session = session
        self.index = 0
        self.clock = float("-inf")
        self.timestamp = ""
        self.cwd = cwd

    def at(self, timestamp) -> None:
        if isinstance(timestamp, str) and timestamp:
            self.timestamp = timestamp[:64]
            session = self.session
            session.first_timestamp = session.first_timestamp or self.timestamp
            session.last_timestamp = self.timestamp
            seconds = epoch(timestamp)
            if seconds is not None and seconds > self.clock:
                self.clock = seconds

    def message(self, msg: dict, ordinal: int) -> None:
        self.at(msg.get("timestamp"))
        kind = msg.get("type")
        if kind == "user":
            text = parts_text(msg.get("content")).strip()
            if text and not text.startswith("<"):
                self.session.prompt_starts.append(self.index)
                self.session.prompt_marks.append((self.index, ordinal))
        elif kind == "gemini":
            text = parts_text(msg.get("content")).strip()
            if text:
                self.session.events.append(Event(
                    kind=EventKind.TEXT, index=self.index, timestamp=self.timestamp,
                    text=text, t=self.clock))
                self.index += 1
            calls = msg.get("toolCalls")
            for call in calls if isinstance(calls, list) else []:
                if isinstance(call, dict):
                    self.tool(call)
                else:
                    self.session.unrecognised += 1

    def tool(self, call: dict) -> None:
        self.at(call.get("timestamp"))
        name = call.get("name") if isinstance(call.get("name"), str) else ""
        name = name[:128]
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        call_id = str(call.get("id", ""))[:256]
        status = call.get("status") if isinstance(call.get("status"), str) else ""
        cwd = self.cwd
        if name in _SHELL_TOOLS:
            command = args.get("command") if isinstance(args.get("command"), str) else ""
            workdir = args.get("dir_path") if isinstance(args.get("dir_path"), str) else ""
            action, tool_input = SHELL, {"command": command}
            cwd = _join(self.cwd, clean_path(workdir))
        elif name in _WRITE_TOOLS:
            action = WRITE
            tool_input = {"file_path": _str(args.get("file_path")),
                          "content": _str(args.get("content"))}
        elif name in _EDIT_TOOLS:
            action = EDIT
            tool_input = {"file_path": _str(args.get("file_path")),
                          "old_string": _str(args.get("old_string")),
                          "new_string": _str(args.get("new_string"))}
        else:
            action, tool_input = OTHER, args
        event = Event(kind=EventKind.TOOL_CALL, index=self.index, timestamp=self.timestamp,
                      tool_name=name, action=action, tool_id=call_id, tool_input=tool_input,
                      t=self.clock, cwd=cwd, result_seen=False)
        if action == SHELL:
            event.check_types = check_types(event.command)
        self.session.events.append(event)
        self.index += 1
        self.result(event, status, call)

    @staticmethod
    def _hints(event: Event) -> None:
        event.failure_hint = bool(FAILURE_IN_OUTPUT.search(event.output))
        event.pass_hint = bool(PASS_IN_OUTPUT.search(event.output))
        event.passed_counts = passed_counts(event.output) if event.check_types else frozenset()

    def result(self, event: Event, status: str, call: dict) -> None:
        if status not in _ENDED:
            event.status = status[:32] if status in _WAITING else ""
            return  # no result in the log: result_seen stays False
        text, has_error = result_text(call.get("result"))
        if not text and isinstance(call.get("resultDisplay"), str):
            text = call["resultDisplay"]
        event.result_seen = True
        if event.tool_action != SHELL:
            event.output = unwrap(text)
            event.status = status
            event.is_error = status == "error" or has_error
            return
        code, body = shell_result(text) if status != "error" else (None, unwrap(text))
        event.output = body
        if code is not None:
            event.exit_code = code
            event.is_error = True
            event.status = "failed"
        elif status == "success" and body.startswith(_BACKGROUND):
            event.background = True  # it went on in the background: no evidence yet
            event.check_types = frozenset()
            event.status = "running"
        else:
            event.status = status  # success, error or cancelled, with no exit code
            event.is_error = status == "error"
        self._hints(event)


def parse_session(path: str | Path) -> Session:
    path = Path(path)
    if is_remote_path(path):
        raise UnreadableSession("a network path is never read")
    session = Session(path=str(path))
    history = _History(session)
    if path.name.endswith(".json"):
        record = _read_json(path)
        if record is None:
            raise UnreadableSession("an older .json session that is not readable "
                                    "(too big, not JSON, or not a Gemini session)")
        history.add(record)
    else:
        with open(path, "rb") as fh:
            for line in _lines(fh, MAX_LINE):
                if line is None:
                    session.unrecognised += 1
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    record = loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    session.unrecognised += 1
                    continue
                history.add(record)
    meta = history.meta
    session.session_id = _str(meta.get("sessionId"))[:128]
    # the label is the id `audit` takes for this file (QA-028 g): the file name's
    # id, whole, since the full sessionId inside the file is not a target
    session.slug = short_id(Path(path).name) or session.session_id
    session.cwd = project_root(path)
    build = _Build(session, session.cwd)
    build.at(meta.get("startTime"))
    for ordinal, msg in enumerate(history.messages.values()):
        build.message(msg, ordinal)
    return session


def _full_id(path: Path) -> tuple[str, ...]:
    meta = first_record(path)
    return (meta["sessionId"],) if meta else ()


class GeminiReader:
    name = "gemini"
    label = "Gemini CLI"  # the agent as messages name it

    @property
    def location(self) -> str:
        return ("$GEMINI_CLI_HOME/.gemini/tmp" if os.environ.get("GEMINI_CLI_HOME")
                else "~/.gemini/tmp")

    def discover(self, project_filter: str | None = None) -> list[Path]:
        root = tmp_dir()
        if root is None or not root.is_dir():
            return []
        found: list[tuple[float, Path]] = []
        for project in _entries(root):
            if not (_PROJECT.match(project.name) and _is_plain_dir(project)):
                continue
            for chats in _entries(Path(project.path)):
                if chats.name == "chats" and _is_plain_dir(chats):
                    found.extend(_sessions_in(Path(chats.path)))
        found.sort(key=lambda pair: pair[0], reverse=True)
        paths = [p for _, p in found]
        if project_filter:
            wanted = project_filter.lower()
            paths = [p for p in paths if wanted in self._project(p).lower()]
        return paths

    @staticmethod
    def _project(path: Path) -> str:
        """The recorded project path, else the project folder's name."""
        return project_root(path) or Path(path).parent.parent.name

    def resolve(self, target: str, project_filter: str | None = None) -> Path:
        direct = Path(target)
        if not is_remote_path(target) and direct.is_file():
            return direct
        sessions = self.discover(project_filter)
        try:
            return pick_session(sessions, target, self.session_keys, self.location)
        except FileNotFoundError:
            if len(target) <= 8:
                raise
        # a longer id than the file name holds: the full id in each session's first record
        return pick_session(sessions, target, _full_id, self.location)

    def session_keys(self, path: Path) -> tuple[str, ...]:
        return short_id(path.name), _stem(path.name)

    def detect(self, path: Path) -> bool:
        return first_record(Path(path)) is not None

    def describe(self, path: Path) -> tuple[str, str]:
        project = self._project(path)
        name = re.split(r"[\\/]", project.rstrip("\\/"))[-1] if project else "?"
        return short_id(path.name)[:8], _UNSAFE.sub("?", name)[:60]

    def parse(self, path: Path) -> Session:
        return parse_session(path)

    def attach_sources(self, session: Session) -> Session:
        return session  # sub-agent chats are not read for Gemini yet


GEMINI = GeminiReader()
