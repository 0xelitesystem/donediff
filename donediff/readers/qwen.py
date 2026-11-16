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

Lines are read in binary, one at a time, capped at MAX_LINE bytes; a longer
line, a line that is not JSON and a record this reader does not know are
skipped and counted in Session.unrecognised. The reader starts no process and
makes no network call; paths it reads from the log are checked later through
donediff.shell.confined like every other transcript path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..evidence import FAILURE_IN_OUTPUT, PASS_IN_OUTPUT, check_types, passed_counts
from ..models import EDIT, OTHER, SHELL, WRITE, Event, EventKind, Session
from ..parser import epoch, loads
from ..shell import is_remote_path
from .base import UnreadableSession
from .codex import _UNSAFE, _join, _lines, clean_path
from .gemini import _entries, _is_plain_dir, result_text

MAX_LINE = 32 * 1024 * 1024  # bytes in one log line; a longer line is skipped
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
_TRAILER = re.compile(
    r"^Error: (.*)\nExit Code: (-?\d+|\(none\))\nSignal: [^\n]*\nProcess Group PGID: [^\n]*$",
    re.M)
_FINISHED = ("Command: ", "Directory: ")  # how a finished (not cancelled) result starts
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


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
    """
    if not text.startswith(_FINISHED):
        return None, text
    matches = list(_TRAILER.finditer(text))
    if not matches:
        return None, text
    last = matches[-1]
    code = None if last.group(2) == "(none)" else int(last.group(2))
    head = text[:last.start()]
    start = head.find("Output: ")
    body = head[start + len("Output: "):] if start >= 0 else head
    body = body[:-1] if body.endswith("\n") else body
    return code, "" if body == "(empty)" else body


# -- the reader ---------------------------------------------------------------

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

    def record(self, record: dict, ordinal: int) -> None:
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

    def tool(self, call: dict) -> None:
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
            action, tool_input = OTHER, args
        event = Event(kind=EventKind.TOOL_CALL, index=self.index, timestamp=self.timestamp,
                      tool_name=name, action=action, tool_id=call_id, tool_input=tool_input,
                      t=self.clock, cwd=cwd, result_seen=False)
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
        if not structured:
            code, body = shell_text(text)
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
            event.is_error = code != 0 or status == "error"
            event.status = "failed" if code != 0 else "completed"
        else:
            event.status = status  # success, error or cancelled, with no exit code
            event.is_error = status == "error"
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
                records.append(record)
            else:
                session.unrecognised += 1
    chain = _chain(records)
    for record in chain:
        if isinstance(record.get("sessionId"), str):
            session.session_id = record["sessionId"][:128]
            break
    session.slug = session.session_id[:12]
    build = _Build(session, _results(chain))
    for ordinal, record in enumerate(chain):
        build.record(record, ordinal)
    return session


class QwenReader:
    name = "qwen"

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
        sessions = self.discover(project_filter)
        if target == "latest":
            if not sessions:
                raise FileNotFoundError(f"no transcripts found under {self.location}")
            return sessions[0]
        for candidate in sessions:
            if candidate.stem.startswith(target):
                return candidate
        raise FileNotFoundError(f"no transcript matching {target!r}")

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
