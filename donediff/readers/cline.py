"""The Cline reader: session files under ~/.cline/data/sessions/<id>/.

Format notes (read from github.com/cline/cline, Apache-2.0, branch main at
commit 146894b6a42a; the format is internal and moves):

- Home: the sessions folder is $CLINE_SESSION_DATA_DIR when set, else
  $CLINE_DATA_DIR/sessions, else $CLINE_DIR/data/sessions, else
  <home>/.cline/data/sessions, where <home> is $HOME, else $USERPROFILE
  (sdk/packages/shared/src/storage/paths.ts resolveDefaultHomeDir,
  resolveClineDir, resolveClineDataDir, resolveSessionDataDir). Values are
  trimmed and never "~"-expanded; a relative value joins the current folder.
- Sessions: <sessions>/<id>/<id>.messages.json, with the manifest <id>.json
  beside it (sdk/packages/core/src/services/session-artifacts.ts L75-94).
  Sub-agent files in the same folder have another stem and are not read yet.
  The sessions.db index is never opened: the manifest already holds the
  project path ("cwd", "workspace_root"; session/models/session-manifest.ts).
- File (sdk/packages/core/docs/messages-contract-v1.md, services/
  session-data.ts buildMessagesFilePayload): one JSON document {version: 1,
  updated_at, agent: lead|subagent|teammate, sessionId, messages: [...],
  system_prompt?}, rewritten whole, pretty-printed. A message is {id, role:
  user|assistant, content: [blocks], ts (epoch ms)}; blocks are Anthropic
  style: text, thinking, tool_use {id, name, input}, tool_result
  {tool_use_id, content, is_error?}; a user message may hold a "file" block.
- Tool results (session/persisted-tool-result-content.ts): a list is kept as
  a list, anything else is JSON.stringify'd. Cline's own tools return
  ToolOperationResult {query, result, error?, success}
  (extensions/tools/definitions.ts).
- run_commands (definitions.ts createShellTool L509-562, executeShellCommands
  L180-257; input schemas.ts L127-176): one call runs a list of commands and
  returns one {query, result, success} per command. A command that exits 0
  gives success: true and its output, with NO exit code written
  (executors/bash.ts L1009-1020). A non-zero exit throws CommandExitError,
  whose message is "Command exited with code N" (bash.ts L321-329,
  L984-1007); it is stored as error with success: false. A signal, a
  timeout or a spawn failure gives success: false and no code. So a pass is a
  status only (never "exit 0"); a failure with that exact error line carries
  its real exit code; any other failure is status "error".
- editor (definitions.ts L711-767, executors/editor.ts L221-267): path,
  old_text, new_text, insert_line. With old_text it is an edit; insert_line
  is an edit that adds new_text; with neither it creates the file (a write).
  apply_patch (definitions.ts L662-704): {input: patch} or the patch string,
  the same "*** Begin Patch" grammar Codex uses. Paths are as the model sent
  them (the tools resolve a relative one against the session cwd).

- Only the tool's own fields give a status: a run_commands result that is a
  string (not Cline's list) is output text, so a "success" or "Command
  exited with code 0" printed by a command never counts; is_error on the
  block, success: false or an error field always wins over a success flag,
  and when a call has more than one result block a failing copy wins.
- History: the file holds the active history only. A chat checkpoint restore
  rewrites it without the later messages (session/checkpoint-restore.ts
  trimMessagesToCheckpoint L245-251) and a compaction folds old messages into
  a summary (conversation-store.ts replaceMessages); nothing dropped stays in
  the file, so no dropped record can back a claim. A workspace-only restore
  keeps the messages while the files go back: the disk check (not this
  reader) is what catches an edit that is no longer there.

The file is read in binary, capped at MAX_FILE bytes; a file that is larger
or not JSON is an unreadable session (reported, not a crash). A message or
block this reader does not know is skipped and counted in
Session.unrecognised. The reader starts no process and makes no network call;
paths it reads from the log are checked later through donediff.shell.confined
like every other transcript path.
"""

from __future__ import annotations

import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from ..evidence import FAILURE_IN_OUTPUT, PASS_IN_OUTPUT, check_types, passed_counts
from ..models import EDIT, MULTI_EDIT, OTHER, SHELL, WRITE, Event, EventKind, Session
from ..parser import loads
from ..shell import is_remote_path
from .base import UnreadableSession
from .codex import _UNSAFE, argv_command, clean_path, parse_patch
from .gemini import _entries, _is_plain_dir

MAX_FILE = 128 * 1024 * 1024  # bytes in one session file; a larger one is not read
_MANIFEST_LIMIT = 4 * 1024 * 1024  # bytes of the manifest beside it

_SUFFIX = ".messages.json"
_ID = re.compile(r"^[0-9A-Za-z_-]{1,128}$")
_AGENTS = frozenset({"lead", "subagent", "teammate"})
_KNOWN_BLOCKS = frozenset({"text", "thinking", "redacted_thinking", "tool_use",
                           "tool_result", "image", "file"})
_SHELL_TOOLS = frozenset({"run_commands"})
_EDITOR_TOOLS = frozenset({"editor"})
_PATCH_TOOLS = frozenset({"apply_patch"})
# The message of bash.ts CommandExitError, stored as the result's "error".
_EXITED = re.compile(r"Command exited with code (-?\d+)")
_NOTICE = re.compile(r"^\[Command exited with code -?\d+\]\n?")
_WORKDIR = re.compile(r"^\d+\. Working Directory: (.+)$", re.M)
# Cline's own text for a tool call that never ran, as the whole is_error result
# with no ToolOperationResult (a run_commands result is otherwise always a
# list): the user denied the approval (apps/vscode/src/sdk/tool-approval-denial.ts
# at 146894b6a42a: EDIT_TOOL_APPROVAL_DENIAL_REASON, and the default
# USER_REJECTED_TOOL_REASON from @cline/shared, "The user denied this operation."
# as in the SEC-212 fixtures; any feedback follows), or the call was cancelled
# before it ran (USER_MESSAGE_TOOL_APPROVAL_DENIAL_REASON, "Tool execution was
# cancelled because ..."). Status words are in verify.NO_EVIDENCE_STATUS (D087).
_NOT_RUN = (("The user denied this operation", "declined"),
            ("The user denied this edit", "declined"),
            ("Tool execution was cancelled", "cancelled"))


def _not_run(text: str) -> str | None:
    head = text.lstrip()
    return next((status for start, status in _NOT_RUN if head.startswith(start)), None)


# -- where the sessions are ---------------------------------------------------

def _home() -> str:
    home = os.environ.get("HOME", "").strip()
    if home and home != "~":
        return home
    return os.environ.get("USERPROFILE", "").strip() or os.path.expanduser("~")


def sessions_dir() -> Path | None:
    """The sessions folder, or None when it would be a network path."""
    for name, tail in (("CLINE_SESSION_DATA_DIR", ()), ("CLINE_DATA_DIR", ("sessions",)),
                       ("CLINE_DIR", ("data", "sessions"))):
        env = os.environ.get(name, "").strip()
        if env:
            root = Path(os.path.abspath(env)).joinpath(*tail)
            if is_remote_path(env) or is_remote_path(root):
                return None
            return root
    root = Path(os.path.abspath(_home())) / ".cline" / "data" / "sessions"
    return None if is_remote_path(root) else root


def _session_id(path: Path) -> str:
    name = Path(path).name
    return name[:-len(_SUFFIX)] if name.endswith(_SUFFIX) else Path(path).stem


def is_cline_document(doc) -> bool:
    """True for a Cline messages file (and never for a Gemini CLI or Claude Code one)."""
    return (isinstance(doc, dict) and doc.get("version") == 1
            and not isinstance(doc.get("version"), bool)
            and isinstance(doc.get("messages"), list)
            and isinstance(doc.get("sessionId"), str)
            and doc.get("agent") in _AGENTS
            and "projectHash" not in doc and "type" not in doc)


def _read_json(path: Path, limit: int):
    """The JSON document in a regular file of at most `limit` bytes; ValueError otherwise."""
    if is_remote_path(path):
        raise UnreadableSession("a network path is never read")
    with open(path, "rb") as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too large")
    return loads(data.decode("utf-8-sig", errors="replace"))  # too deep: ValueError (SEC-207)


def read_document(path: Path) -> dict | None:
    """The session document when the file is a Cline messages file, else None."""
    path = Path(path)
    if is_remote_path(path):
        return None
    try:
        with open(path, "rb") as fh:
            head = fh.read(64).lstrip(b"\xef\xbb\xbf \t\r\n")
        if not head.startswith(b"{"):
            return None
        doc = _read_json(path, MAX_FILE)
    except (OSError, ValueError):
        return None
    return doc if is_cline_document(doc) else None


def manifest(path: Path) -> dict:
    """The <id>.json manifest beside a messages file, or {} (missing, linked, too big, bad)."""
    target = Path(path).parent / f"{_session_id(path)}.json"
    if is_remote_path(target):
        return {}
    try:
        if not stat.S_ISREG(os.lstat(target).st_mode):
            return {}  # a symlink or anything else is not followed
        data = _read_json(target, _MANIFEST_LIMIT)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("version") == 1 else {}


# -- what a message holds -----------------------------------------------------

def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _iso(ms) -> str:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return ""
    try:
        moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def commands_of(tool_input) -> list[str]:
    """run_commands' command lines, from every input shape schemas.ts accepts."""
    value = tool_input
    if isinstance(value, dict):
        for key in ("commands", "command", "cmd"):
            if key in value:
                value = value if key == "command" and "args" in value else value[key]
                break
    items = value if isinstance(value, list) else [value]
    commands = []
    for item in items:
        if isinstance(item, str):
            commands.append(item)
        elif isinstance(item, dict) and isinstance(item.get("command"), str):
            args = item.get("args")
            args = args if isinstance(args, list) and all(isinstance(a, str) for a in args) else []
            commands.append(argv_command([item["command"], *args]))
    return commands


def operations(content, from_text: bool = False) -> tuple[list[dict], str]:
    """(ToolOperationResult objects, plain text) of a tool_result's content.

    A string is read as a stringified result only with from_text, for the
    tools whose single result Cline JSON-stringifies (editor, apply_patch).
    run_commands results are always a list (persisted-tool-result-content.ts),
    so a string there is output text: a "success" printed by a command is
    never read as the tool's own field.
    """
    if isinstance(content, str):
        if not from_text:
            return [], content
        try:
            parsed = loads(content) if content.lstrip()[:1] in ("{", "[") else None
        except ValueError:
            parsed = None
        if parsed is None:
            return [], content
        content = parsed
    items = content if isinstance(content, list) else [content]
    ops, texts = [], []
    for item in items:
        if not isinstance(item, dict):
            if isinstance(item, str):
                texts.append(item)
            continue
        if "success" in item or "query" in item:
            ops.append(item)
        elif isinstance(item.get("text"), str):
            texts.append(item["text"])
    return ops, "\n".join(t for t in texts if t)


def _op_text(op: dict) -> str:
    return "\n".join(t for t in (_str(op.get("result")), _str(op.get("error"))) if t)


# -- the reader ---------------------------------------------------------------

class _Build:
    """Events from the messages, in order."""

    def __init__(self, session: Session, results: dict[str, list[dict]]):
        self.session = session
        self.results = results
        self.index = 0
        self.clock = float("-inf")
        self.timestamp = ""

    def at(self, message: dict) -> None:
        timestamp = _iso(message.get("ts"))
        if timestamp:
            self.timestamp = timestamp
            session = self.session
            session.first_timestamp = session.first_timestamp or timestamp
            session.last_timestamp = timestamp
            seconds = message["ts"] / 1000
            if seconds > self.clock:
                self.clock = seconds

    def message(self, message, ordinal: int) -> None:
        if not (isinstance(message, dict) and message.get("role") in ("user", "assistant")
                and isinstance(message.get("content"), list)):
            self.session.unrecognised += 1
            return
        self.at(message)
        blocks = message["content"]
        for block in blocks:
            if not (isinstance(block, dict) and block.get("type") in _KNOWN_BLOCKS):
                self.session.unrecognised += 1
        if message["role"] == "user":
            text = "\n".join(b["text"] for b in blocks if isinstance(b, dict)
                             and b.get("type") == "text" and isinstance(b.get("text"), str))
            text = text.strip()
            if text and not text.startswith("<"):
                self.session.prompt_starts.append(self.index)
                self.session.prompt_marks.append((self.index, ordinal))
            return
        texts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "tool_use":
                self.text("\n".join(texts))
                texts = []
                self.tool(block)
        self.text("\n".join(texts))

    def text(self, text: str) -> None:
        text = text.strip()
        if text:
            self.session.events.append(Event(
                kind=EventKind.TEXT, index=self.index, timestamp=self.timestamp,
                text=text, t=self.clock))
            self.index += 1

    def _event(self, name: str, action: str, call_id: str, tool_input: dict) -> Event:
        event = Event(kind=EventKind.TOOL_CALL, index=self.index, timestamp=self.timestamp,
                      tool_name=name, action=action, tool_id=call_id, tool_input=tool_input,
                      t=self.clock, cwd=self.session.cwd, result_seen=False)
        if action == SHELL:
            event.check_types = check_types(event.command)
        self.session.events.append(event)
        self.index += 1
        return event

    def tool(self, block: dict) -> None:
        name = _str(block.get("name"))[:128]
        call_id = str(block.get("id", ""))[:256]
        raw = block.get("input")
        args = raw if isinstance(raw, dict) else {}
        found = self.results.get(call_id, []) if call_id else []
        if name in _SHELL_TOOLS:
            events = [self._event(name, SHELL, call_id, {"command": c})
                      for c in commands_of(raw)] or [self._event(name, OTHER, call_id, args)]
            for result in found:  # a failure in any copy of the result wins
                self.shell_results(events, result)
            return
        if name in _EDITOR_TOOLS:
            path = clean_path(args.get("path"))
            new = _str(args.get("new_text"))
            if args.get("insert_line") is not None:
                events = [self._event(name, EDIT, call_id, {
                    "file_path": path, "old_string": "", "new_string": new})]
            elif isinstance(args.get("old_text"), str):
                events = [self._event(name, EDIT, call_id, {
                    "file_path": path, "old_string": args["old_text"], "new_string": new})]
            else:
                events = [self._event(name, WRITE, call_id, {"file_path": path, "content": new})]
        elif name in _PATCH_TOOLS:
            patch = raw if isinstance(raw, str) else _str(args.get("input"))
            events = []
            for op, path, data in parse_patch(patch):
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
                        events.append(self._event(name, MULTI_EDIT, call_id, {
                            "file_path": path,
                            "edits": [{"old_string": o, "new_string": n} for o, n in hunks]}))
                else:
                    events.append(self._event(name, OTHER, call_id, {"file_path": path}))
            events = events or [self._event(name, OTHER, call_id, {})]
        else:
            events = [self._event(name, OTHER, call_id, args)]
        for result in found:
            self.plain_result(events, result, name in _EDITOR_TOOLS or name in _PATCH_TOOLS)

    @staticmethod
    def plain_result(events: list[Event], block: dict, stringified: bool) -> None:
        ops, text = operations(block.get("content"), from_text=stringified)
        failed = block.get("is_error") is True or any(
            op.get("success") is not True or _str(op.get("error")) for op in ops)
        output = "\n".join(t for t in [*(_op_text(op) for op in ops), text] if t)
        for event in events:
            if event.is_error:
                continue  # an earlier copy failed: a later success does not undo it
            event.result_seen = True
            event.output = output
            # No ToolOperationResult and no is_error: the log does not show the
            # write landed, so its outcome is unknown, never a success (SEC-212).
            event.status = "error" if failed else "success" if ops else "unknown"
            event.is_error = failed

    def shell_results(self, events: list[Event], block: dict) -> None:
        ops, text = operations(block.get("content"))
        if block.get("is_error") is True:
            # The tool call itself failed: every command is an error, whatever
            # success-looking entry the content holds; an exact exit line still counts.
            ops = [{**op, "success": False} for op in ops]
        if len(ops) != len(events):
            # Commands and results no longer line up (Cline joins heredoc
            # lines before it runs them): pair only a result whose query is
            # the command itself; the rest stay without a result.
            by_query: dict[str, dict] = {}
            for op in ops:
                by_query.setdefault(_str(op.get("query")), op)
            pairs = [(e, by_query.get(e.command)) for e in events]
        else:
            pairs = list(zip(events, ops))
        if not ops and block.get("is_error") is True and _not_run(text):
            for event in events:
                if not event.is_error:  # an earlier failing copy still wins
                    event.mark_not_run(_not_run(text), text)  # never ran: no evidence
            return
        if not ops and block.get("is_error") is True:
            pairs = [(e, {"success": False, "error": text}) for e in events]  # the call failed
        elif not ops and text:
            pairs = [(e, {"result": text}) for e in events]  # text only: status unknown
        for event, op in pairs:
            if op is not None:
                self.shell_result(event, op)

    @staticmethod
    def shell_result(event: Event, op: dict) -> None:
        if event.is_error:
            return  # an earlier copy failed: a later success does not undo it
        event.result_seen = True
        success = op.get("success")
        output = _str(op.get("result"))
        error = _str(op.get("error"))
        if success is True and error:
            success = False  # Cline writes "error" only on a failure: the failure wins
        if success is True:
            event.status = "success"  # exit 0 is not written: a status only
        elif success is False:
            exited = _EXITED.fullmatch(error)
            code = int(exited.group(1)) if exited else 0
            if code:
                event.exit_code = code
                event.status = "failed"
                output = _NOTICE.sub("", output, count=1)
            else:
                event.status = "error"  # signal, timeout or spawn failure: no exit code
            event.is_error = True
        else:
            event.status = "unknown"
        event.output = output if success is not False or output else error
        event.failure_hint = bool(FAILURE_IN_OUTPUT.search(event.output))
        event.pass_hint = bool(PASS_IN_OUTPUT.search(event.output))
        event.passed_counts = passed_counts(event.output) if event.check_types else frozenset()


def _results(messages: list) -> dict[str, list[dict]]:
    """tool_use id -> its tool_result blocks, in order (a failing copy wins later)."""
    found: dict[str, list[dict]] = {}
    for message in messages:
        if not (isinstance(message, dict) and message.get("role") == "user"
                and isinstance(message.get("content"), list)):
            continue
        for block in message["content"]:
            if (isinstance(block, dict) and block.get("type") == "tool_result"
                    and isinstance(block.get("tool_use_id"), str)):
                found.setdefault(block["tool_use_id"][:256], []).append(block)
    return found


def project_of(path: Path, doc: dict | None = None) -> str:
    """The project path: the manifest's cwd, else its workspace_root, else the
    "Working Directory" line of the system prompt Cline wrote."""
    meta = manifest(path)
    for key in ("cwd", "workspace_root"):
        value = clean_path(meta.get(key))
        if value:
            return value
    if doc is None:
        doc = read_document(path)
    match = _WORKDIR.search(_str(doc.get("system_prompt"))) if doc else None
    return clean_path(match.group(1).strip()) if match else ""


def parse_session(path: str | Path) -> Session:
    path = Path(path)
    if is_remote_path(path):
        raise UnreadableSession("a network path is never read")
    try:
        doc = _read_json(path, MAX_FILE)
    except ValueError:
        raise UnreadableSession(
            f"a Cline session file that is not readable JSON (or over "
            f"{MAX_FILE // (1024 * 1024)} MiB): {path.name}") from None
    if not is_cline_document(doc):
        raise UnreadableSession(f"not a Cline session file (version 1): {path.name}")
    session = Session(path=str(path))
    session.session_id = doc["sessionId"][:128]
    session.slug = session.session_id[:12]
    session.cwd = project_of(path, doc)
    if isinstance(doc.get("updated_at"), str):
        session.last_timestamp = doc["updated_at"][:64]
    messages = doc["messages"]
    build = _Build(session, _results(messages))
    for ordinal, message in enumerate(messages):
        build.message(message, ordinal)
    return session


class ClineReader:
    name = "cline"

    @property
    def location(self) -> str:
        for name, tail in (("CLINE_SESSION_DATA_DIR", ""), ("CLINE_DATA_DIR", "/sessions"),
                           ("CLINE_DIR", "/data/sessions")):
            if os.environ.get(name, "").strip():
                return f"${name}{tail}"
        return "~/.cline/data/sessions"

    def discover(self, project_filter: str | None = None) -> list[Path]:
        root = sessions_dir()
        if root is None or not root.is_dir():
            return []
        found: list[tuple[float, Path]] = []
        for entry in _entries(root):
            if not (_ID.match(entry.name) and _is_plain_dir(entry)):
                continue
            target = Path(entry.path) / f"{entry.name}{_SUFFIX}"
            try:
                info = os.lstat(target)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):  # a symlinked file is not followed
                found.append((info.st_mtime, target))
        found.sort(key=lambda pair: pair[0], reverse=True)
        paths = [p for _, p in found]
        if project_filter:
            wanted = project_filter.lower()
            paths = [p for p in paths if wanted in project_of(p).lower()]
        return paths

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
            if _session_id(candidate).startswith(target):
                return candidate
        raise FileNotFoundError(f"no transcript matching {target!r}")

    def detect(self, path: Path) -> bool:
        return read_document(Path(path)) is not None

    def describe(self, path: Path) -> tuple[str, str]:
        project = project_of(path)
        name = re.split(r"[\\/]", project.rstrip("\\/"))[-1] if project else "?"
        return _UNSAFE.sub("?", _session_id(path)[:8]), _UNSAFE.sub("?", name)[:60]

    def parse(self, path: Path) -> Session:
        return parse_session(path)

    def attach_sources(self, session: Session) -> Session:
        return session  # sub-agent and teammate files are not read for Cline yet


CLINE = ClineReader()
