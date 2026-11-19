"""The Qwen Code reader: chat files under ~/.qwen/projects/<project>/chats.

Format notes (read from github.com/QwenLM/qwen-code, Apache-2.0, branch main
at commit 516e963f7a40; the format is internal and moves):

- Home: the runtime folder is $QWEN_RUNTIME_DIR when set, else $QWEN_HOME,
  else ~/.qwen; "~" is expanded and a relative value joins the current folder
  (packages/core/src/config/storage.ts getRuntimeBaseDir, getGlobalQwenDir,
  resolvePath). A runtime folder set only in Qwen's settings file is not
  followed: this reader never opens that file.
- Sessions: <runtime>/projects/<project id>/chats/<session id>.jsonl
  (storage.ts getProjectDir, chatRecordingService.ts ensureConversationFile).
  The project id is the project path with every character that is not a
  letter or digit made "-", lowercased on Windows (utils/paths.ts
  sanitizeCwd); every record also carries the "cwd" it ran in. The class
  comment in chatRecordingService.ts still names tmp/<project id>/chats, so
  <runtime>/tmp/<id>/chats is read too.
- Lines (chatRecordingService.ts ChatRecord): {uuid, parentUuid, sessionId,
  timestamp, type: user|assistant|tool_result|system, subtype?, cwd, version,
  message?: {role, parts}, toolCallResult?, systemPayload?}. An assistant
  record's parts hold text (thought: true parts are reasoning) and
  functionCall {id, name, args}; a tool_result record's parts hold
  functionResponse {id, name, response: {output} or {error}} with
  toolCallResult {callId, status, resultDisplay, ...}. Records form a tree
  through parentUuid: a rewind re-roots the chain, so the history is the path
  from the last record back to the root (rewindRecording), which this reader
  follows like Qwen's own resume does. A "user" record with a subtype
  (notification, cron, ...) is not a typed prompt.
- run_shell_command (tools/shell.ts, args command, is_background, directory):
  a finished run writes, after "Output: ...", the lines "Error: ...",
  "Exit Code: <N or (none)>", "Signal: ...", "Process Group PGID: ..." on
  every run, exit 0 included (shell.ts L4190-4200), and its resultDisplay is
  a structured {type: "shell_result", version: 1, exitCode, outcome, output,
  ...} (utils/shell-result.ts) that the recorder keeps
  (utils/toolResultDisplayCompaction.ts). So Qwen logs a real exit code for
  a completed run, 0 included: the structured exitCode is read first, the
  text trailer only when there is no structured display. A cancelled or
  timed-out run has no exit code; a background start ("Background shell
  started.") is no evidence yet.
- write_file (file_path, content) is a write, edit (file_path, old_string,
  new_string; legacy name replace) an edit; every other tool keeps its name.

A session file over MAX_FILE bytes is not read. Lines are read in binary, one
at a time, capped at MAX_LINE bytes, and each record kept for the chain holds
only the fields the reader reads, as text (NEW2-I-1); a longer
line, a line that is not JSON and a record this reader does not know are
skipped and counted in Session.unrecognised. The reader starts no process and
makes no network call; paths it reads from the log are checked later through
donediff.shell.confined like every other transcript path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..evidence import FAILURE_IN_OUTPUT, PASS_IN_OUTPUT, check_types, log_int, passed_counts
from ..models import EDIT, OTHER, SHELL, WRITE, Event, EventKind, Session
from ..parser import epoch, kept_input, loads
from ..shell import is_remote_path
from .base import UnreadableSession, _int, _str, pick_session
from .codex import _UNSAFE, _join, _lines, clean_path
from .gemini import _entries, _is_plain_dir, result_text

MAX_LINE = 32 * 1024 * 1024  # bytes in one log line; a longer line is skipped
MAX_FILE = 512 * 1024 * 1024  # bytes of one session file, as parser.MAX_FILE; larger is not read
_META_LIMIT = 4 * 1024 * 1024  # bytes of the first line read for discovery and detect

_FILE = re.compile(r"^[0-9A-Za-z_-]{1,128}\.jsonl$")
_PROJECT = re.compile(r"^[0-9A-Za-z_.-]{1,255}$")
_TYPES = frozenset({"user", "assistant", "tool_result", "system"})
_ENDED = frozenset({"success", "error", "cancelled"})
_SHELL_TOOLS = frozenset({"run_shell_command"})
_WRITE_TOOLS = frozenset({"write_file"})
_EDIT_TOOLS = frozenset({"edit", "replace"})
_CLAUDE_KEYS = ("isSidechain", "userType", "toolUseResult", "payload")
# The trailer shell.ts writes on every finished run (L4196-4199), last copy wins.
# The real "Error: " text before it can span lines, so it is not part of the
# match (SEC-209): the command's output sits before the real trailer, so the
# last match is always the real one. Not anchored at the end of the text:
# Qwen appends notices after it, separated by a blank line (L4474, L4541).
_TRAILER = re.compile(
    r"^Exit Code: (-?\d+|\(none\))\nSignal: [^\n]*\nProcess Group PGID: [^\n]*$", re.M)
_FINISHED = ("Command: ", "Directory: ")  # how a finished (not cancelled) result starts
_OUTPUT = re.compile(r"^Output: ", re.M)  # the line that opens the output body
_BACKGROUND = "Background shell started."


# -- where the sessions are ---------------------------------------------------

def _resolve(value: str) -> Path:
    if value == "~" or value.startswith(("~/", "~\\")):
        value = os.path.join(os.path.expanduser("~"), value[2:])
    return Path(os.path.abspath(value))  # a relative value joins the current folder


def qwen_dir() -> Path | None:
    """The runtime folder, or None when the env value is remote or missing."""
    for name in ("QWEN_RUNTIME_DIR", "QWEN_HOME"):
        env = os.environ.get(name)
        if env:
            root = _resolve(env)
            if is_remote_path(env) or is_remote_path(root) or not root.is_dir():
                return None
            return root
    root = Path(os.path.expanduser("~")) / ".qwen"
    return None if is_remote_path(root) else root


def _sessions_in(chats: Path) -> list[tuple[float, Path]]:
    found = []
    for entry in _entries(chats):
        if _FILE.match(entry.name) and entry.is_file(follow_symlinks=False):
            try:
                found.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
            except OSError:
                continue
    return found


def is_qwen_record(record) -> bool:
    """True for a Qwen Code chat record (and never for a Claude Code one)."""
    if not (isinstance(record, dict) and isinstance(record.get("uuid"), str)
            and isinstance(record.get("sessionId"), str) and "parentUuid" in record
            and record.get("type") in _TYPES):
        return False
    if any(key in record for key in _CLAUDE_KEYS):
        return False
    message = record.get("message")
    return (record["type"] == "tool_result" or isinstance(record.get("systemPayload"), dict)
            or (isinstance(message, dict) and isinstance(message.get("parts"), list)
                and "content" not in message))


def first_record(path: Path) -> dict | None:
    """The first record of a session file when it is a Qwen Code record, else None."""
    path = Path(path)
    if is_remote_path(path):
        return None
    try:
        with open(path, "rb") as fh:
            for line in _lines(fh, _META_LIMIT):
                if line is None:
                    return None
                line = line.strip()
                if not line:
                    continue
                record = loads(line.decode("utf-8", errors="replace"))
                return record if is_qwen_record(record) else None
    except (OSError, ValueError):
        return None
    return None


# -- what a record holds ------------------------------------------------------

def _parts(record: dict) -> list:
    message = record.get("message")
    parts = message.get("parts") if isinstance(message, dict) else None
    return parts if isinstance(parts, list) else []


def shell_display(display) -> tuple[bool, int | None, str]:
    """(is a shell_result, exit code, output) from the structured resultDisplay.

    The code counts only for a run whose outcome is completed or failed: a
    cancelled or timed-out run has none.
    """
    if not (isinstance(display, dict) and display.get("type") == "shell_result"
            and display.get("version") == 1):
        return False, None, ""
    code = _int(display.get("exitCode"))
    if display.get("outcome") not in ("completed", "failed"):
        code = None
    return True, code, _str(display.get("output"))


def shell_text(text: str) -> tuple[int | None, str]:
    """(exit code, output body) from run_shell_command's own result text.

    Read only from a finished result (it starts "Command: " or "Directory: ")
    whose last trailer block is the one shell.ts writes; "(none)" is no code.
    The trailer must follow the last "Output: " line: shell.ts writes it after
    the whole output, so one with output after it was printed by the command
    (SEC-211).
    """
    if not text.startswith(_FINISHED):
        return None, text
    matches = list(_TRAILER.finditer(text))
    outputs = list(_OUTPUT.finditer(text))
    if not matches or not outputs or matches[-1].start() < outputs[-1].end():
        return None, text
    last = matches[-1]
    code = None if last.group(1) == "(none)" else log_int(last.group(1), 1)
    head = text[:last.start()]
    cut = head.rfind("\nError: ")  # the real Error line(s) sit right before Exit Code
    head = head[:cut + 1] if cut >= 0 else head
    start = head.find("Output: ")
    body = head[start + len("Output: "):] if start >= 0 else head
    body = body[:-1] if body.endswith("\n") else body
    return code, "" if body == "(empty)" else body


# -- the reader ---------------------------------------------------------------

_ARG_KEYS = ((_SHELL_TOOLS, ("command", "directory", "is_background")),
             (_WRITE_TOOLS, ("file_path", "content")),
             (_EDIT_TOOLS, ("file_path", "old_string", "new_string")))


_SCALAR = (str, int, float, bool, type(None))
_RECORD_TEXT = ("parentUuid", "sessionId", "timestamp", "cwd")  # read only when text
_DISPLAY_KEYS = ("type", "version", "exitCode", "outcome", "output")  # shell_display reads these


def _lean_call(call: dict) -> dict:
    """A functionCall with only the keys and args _Build reads (QA-029, NEW2-I-1)."""
    lean = {"name": call["name"]} if isinstance(call.get("name"), str) else {}
    if "id" in call:  # _Build reads str(id)
        lean["id"] = call["id"] if isinstance(call["id"], _SCALAR) else ""
    args = call.get("args")
    if isinstance(args, dict):
        name = _str(call.get("name"))[:128]
        keys = next((keys for tools, keys in _ARG_KEYS if name in tools), None)
        # _Build reads these through _str, and is_background only when True
        lean["args"] = (kept_input(args, edits=False) if keys is None
                        else {key: args[key] for key in keys
                              if isinstance(args.get(key), (str, bool))})
    return lean


def _lean_response(response: dict) -> dict:
    """A functionResponse with only its id and the output / error result_text reads.
    An "error" that is not text stays a key (None): its presence names the status."""
    lean = {"id": response["id"]} if isinstance(response.get("id"), str) else {}
    body = response.get("response")
    if isinstance(body, dict):
        lean["response"] = {key: (body[key] if isinstance(body[key], str) else None)
                            for key in ("output", "error") if key in body}
    return lean


def _lean_part(part):
    """A part with only what _Build and _results read; None when it keeps nothing.
    _Build reads a part's functionCall before its text, so a call part keeps only its call."""
    if not isinstance(part, dict):
        return None
    lean = {}
    if isinstance(part.get("functionCall"), dict):
        lean["functionCall"] = _lean_call(part["functionCall"])
    if isinstance(part.get("functionResponse"), dict):
        lean["functionResponse"] = _lean_response(part["functionResponse"])
    if isinstance(part.get("text"), str):
        lean["text"] = part["text"]
        if part.get("thought") is True:
            lean["thought"] = True
    return lean or None


def _lean(record: dict) -> dict:
    """A record with only the keys, parts and result fields _Build reads, as
    text, so padding anywhere in any record type (a list where text belongs, a
    response's other fields, toolCallResult, an extra key) is not held while
    every record is kept for the chain (QA-029, NEW2-I-1: 18x the file size)."""
    lean = {"uuid": record["uuid"], "type": record["type"]}
    for key in _RECORD_TEXT:
        if isinstance(record.get(key), str):
            lean[key] = record[key]
    if "subtype" in record:  # its presence marks a user record that is not a typed prompt
        lean["subtype"] = record["subtype"] if isinstance(record["subtype"], str) else ""
    parts = _parts(record)
    if parts:
        lean["message"] = {"parts": [p for p in map(_lean_part, parts) if p is not None]}
    info = record.get("toolCallResult")
    if isinstance(info, dict):
        kept = {"status": info["status"]} if isinstance(info.get("status"), str) else {}
        display = info.get("resultDisplay")
        if isinstance(display, dict):
            kept["resultDisplay"] = {key: display[key] for key in _DISPLAY_KEYS
                                     if key in display and isinstance(display[key], _SCALAR)}
        lean["toolCallResult"] = kept
    return lean


def _rewound(records: list[dict], chain: list[dict]) -> dict[str, list[dict]]:
    """Records a rewind left behind, by branch point: off the active history
    but descended from a record on it, so written after it. File order."""
    on_chain = {r["uuid"] for r in chain}
    by_id = {r["uuid"]: r for r in records}
    branch: dict[str, str | None] = {}  # uuid -> its on-chain ancestor, or None
    for record in records:
        trail, current, found = [], record, None
        while current is not None:
            uuid = current["uuid"]
            if uuid in on_chain:
                found = uuid
                break
            if uuid in branch:
                found = branch[uuid]
                break
            branch[uuid] = None  # provisional: also stops a cycle
            trail.append(uuid)
            parent = current.get("parentUuid")
            current = by_id.get(parent) if isinstance(parent, str) else None
        for uuid in trail:
            branch[uuid] = found
    left: dict[str, list[dict]] = {}
    for record in records:
        point = branch.get(record["uuid"])
        if point is not None and record["uuid"] not in on_chain:
            left.setdefault(point, []).append(record)
    return left


def _chain(records: list[dict]) -> list[dict]:
    """The active history: from the last record back to the root via parentUuid."""
    by_id: dict[str, dict] = {}
    for record in records:
        by_id[record["uuid"]] = record
    path, seen = [], set()
    current = records[-1] if records else None
    while current is not None and current["uuid"] not in seen:
        seen.add(current["uuid"])
        path.append(current)
        parent = current.get("parentUuid")
        current = by_id.get(parent) if isinstance(parent, str) else None
    path.reverse()
    return path


class _Build:
    """Events from the active history, in order."""

    def __init__(self, session: Session, results: dict[str, tuple[dict, dict]]):
        self.session = session
        self.results = results
        self.index = 0
        self.clock = float("-inf")
        self.timestamp = ""
        self.cwd = ""

    def at(self, record: dict) -> None:
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            self.timestamp = timestamp[:64]
            session = self.session
            session.first_timestamp = session.first_timestamp or self.timestamp
            session.last_timestamp = self.timestamp
            seconds = epoch(timestamp)
            if seconds is not None and seconds > self.clock:
                self.clock = seconds
        cwd = clean_path(record.get("cwd"))
        if cwd:
            self.cwd = cwd
            self.session.cwd = self.session.cwd or cwd

    def record(self, record: dict, ordinal: int, rewound: bool = False) -> None:
        if rewound:  # evidence-only: its tool calls, never its text or prompts
            if record["type"] == "assistant":
                for part in _parts(record):
                    if isinstance(part, dict) and isinstance(part.get("functionCall"), dict):
                        self.tool(part["functionCall"], rewound=True)
            return
        self.at(record)
        kind = record["type"]
        if kind == "user" and "subtype" not in record:
            text = self._text(_parts(record))
            if text and not text.startswith("<"):
                self.session.prompt_starts.append(self.index)
                self.session.prompt_marks.append((self.index, ordinal))
        elif kind == "assistant":
            texts: list[str] = []
            for part in _parts(record):
                if not isinstance(part, dict):
                    continue
                call = part.get("functionCall")
                if isinstance(call, dict):
                    self.text("\n".join(texts))
                    texts = []
                    self.tool(call)
                elif isinstance(part.get("text"), str) and part.get("thought") is not True:
                    texts.append(part["text"])
            self.text("\n".join(texts))

    @staticmethod
    def _text(parts: list) -> str:
        return "\n".join(p["text"] for p in parts if isinstance(p, dict)
                         and isinstance(p.get("text"), str)
                         and p.get("thought") is not True).strip()

    def text(self, text: str) -> None:
        text = text.strip()
        if text:
            self.session.events.append(Event(
                kind=EventKind.TEXT, index=self.index, timestamp=self.timestamp,
                text=text, t=self.clock))
            self.index += 1

    def tool(self, call: dict, rewound: bool = False) -> None:
        name = _str(call.get("name"))[:128]
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        call_id = str(call.get("id", ""))[:256]
        cwd = self.cwd
        if name in _SHELL_TOOLS:
            action, tool_input = SHELL, {"command": _str(args.get("command"))}
            cwd = _join(self.cwd, clean_path(_str(args.get("directory"))))
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
            action, tool_input = OTHER, kept_input(args, edits=False)  # only what the engine reads (I-1)
        event = Event(kind=EventKind.TOOL_CALL, index=self.index, timestamp=self.timestamp,
                      tool_name=name, action=action, tool_id=call_id, tool_input=tool_input,
                      t=self.clock, cwd=cwd, result_seen=False, rewound=rewound)
        if action == SHELL:
            event.check_types = check_types(event.command)
        self.session.events.append(event)
        self.index += 1
        found = self.results.get(call_id) if call_id else None
        if found is not None:
            self.result(event, args, *found)

    def result(self, event: Event, args: dict, record: dict, response: dict) -> None:
        info = record.get("toolCallResult")
        info = info if isinstance(info, dict) else {}
        status = _str(info.get("status"))
        if status not in _ENDED:
            # No status beside the result: name it from the response shape
            # (coreToolScheduler.ts: {output} on success, {error} on failure).
            body = response.get("response")
            status = "error" if isinstance(body, dict) and "error" in body else "success"
        text, has_error = result_text([{"functionResponse": response}])
        event.result_seen = True
        if event.tool_action != SHELL:
            event.output = text
            event.status = status
            event.is_error = status == "error" or has_error
            return
        structured, code, body = shell_display(info.get("resultDisplay"))
        # A failed outcome is a failure whatever the code says (SEC-209).
        failed_outcome = structured and info["resultDisplay"].get("outcome") == "failed"
        if not structured:
            code, body = shell_text(text)
            if status == "cancelled":
                code = None  # a cancelled run never takes an exit code from its text (SEC-211)
        elif not body:
            body = shell_text(text)[1]
        event.output = body
        if args.get("is_background") is True or text.startswith(_BACKGROUND):
            event.background = True  # it went on in the background: no evidence yet
            event.check_types = frozenset()
            event.status = "running"
            return
        if code is not None:
            event.exit_code = code
            event.is_error = code != 0 or status == "error" or failed_outcome
            event.status = "failed" if code != 0 or failed_outcome else "completed"
        else:
            event.status = status  # success, error or cancelled, with no exit code
            event.is_error = status == "error" or failed_outcome
        event.failure_hint = bool(FAILURE_IN_OUTPUT.search(event.output))
        event.pass_hint = bool(PASS_IN_OUTPUT.search(event.output))
        event.passed_counts = passed_counts(event.output) if event.check_types else frozenset()


def _results(chain: list[dict]) -> dict[str, tuple[dict, dict]]:
    """call id -> (tool_result record, functionResponse) on the active history."""
    found: dict[str, tuple[dict, dict]] = {}
    for record in chain:
        if record["type"] != "tool_result":
            continue
        for part in _parts(record):
            response = part.get("functionResponse") if isinstance(part, dict) else None
            if isinstance(response, dict) and isinstance(response.get("id"), str):
                found[response["id"][:256]] = (record, response)
    return found


def parse_session(path: str | Path) -> Session:
    path = Path(path)
    if is_remote_path(path):
        raise UnreadableSession("a network path is never read")
    session = Session(path=str(path))
    records: list[dict] = []
    if path.stat().st_size > MAX_FILE:  # as parse_transcript does (NEW2-I-1)
        raise UnreadableSession(f"session file is larger than donediff reads "
                                f"({MAX_FILE // (1024 * 1024)} MiB): {path.name}")
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
            if (isinstance(record, dict) and isinstance(record.get("uuid"), str)
                    and record.get("type") in _TYPES):
                records.append(_lean(record))
            else:
                session.unrecognised += 1
    chain = _chain(records)
    for record in chain:
        if isinstance(record.get("sessionId"), str):
            session.session_id = record["sessionId"][:128]
            break
    session.slug = session.session_id
    left = _rewound(records, chain)
    build = _Build(session, _results(chain))
    for uuid, behind in left.items():  # results of rewound calls, never over the chain's
        for call_id, found in _results(behind).items():
            build.results.setdefault(call_id, found)
    for ordinal, record in enumerate(chain):
        build.record(record, ordinal)
        for behind in left.pop(record["uuid"], ()):  # right after its branch point
            build.record(behind, ordinal, rewound=True)
    return session


class QwenReader:
    name = "qwen"
    label = "Qwen Code"  # the agent as messages name it

    @property
    def location(self) -> str:
        for name in ("QWEN_RUNTIME_DIR", "QWEN_HOME"):
            if os.environ.get(name):
                return f"${name}/projects"
        return "~/.qwen/projects"

    def discover(self, project_filter: str | None = None) -> list[Path]:
        root = qwen_dir()
        if root is None or not root.is_dir():
            return []
        found: list[tuple[float, Path]] = []
        for group in ("projects", "tmp"):
            base = root / group
            for project in _entries(base):
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
        """The project path the first record ran in, else the project folder's name."""
        record = first_record(path)
        cwd = clean_path(record.get("cwd")) if record else ""
        return cwd or Path(path).parent.parent.name

    def resolve(self, target: str, project_filter: str | None = None) -> Path:
        direct = Path(target)
        if not is_remote_path(target) and direct.is_file():
            return direct
        return pick_session(self.discover(project_filter), target, self.session_keys,
                            self.location)

    def session_keys(self, path: Path) -> tuple[str, ...]:
        return (Path(path).stem,)

    def detect(self, path: Path) -> bool:
        return first_record(Path(path)) is not None

    def describe(self, path: Path) -> tuple[str, str]:
        project = self._project(path)
        name = re.split(r"[\\/]", project.rstrip("\\/"))[-1] if project else "?"
        return _UNSAFE.sub("?", Path(path).stem[:8]), _UNSAFE.sub("?", name)[:60]

    def parse(self, path: Path) -> Session:
        return parse_session(path)

    def attach_sources(self, session: Session) -> Session:
        return session  # sub-agent sessions are not read for Qwen Code yet


QWEN = QwenReader()
