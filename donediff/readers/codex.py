"""The Codex CLI reader: rollout files under ~/.codex/sessions.

Format notes (read from github.com/openai/codex, codex-rs/rollout and
codex-rs/protocol, at commit 81b995a6c9a4; the format is internal and moves):

- Sessions live in $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl,
  with CODEX_HOME defaulting to ~/.codex. Files 7+ days old may be zstd
  compressed to .jsonl.zst; those are read with the standard library's
  compression.zstd on Python 3.14+, and below that are listed but not read.
- Each line is {"timestamp", "ordinal"?, "type", "payload"}. The first is
  "session_meta" (id, cwd, cli_version, history_mode, git). "turn_context"
  carries the turn's cwd. Only the fields named here are read: the account
  fields and instructions in session_meta are never touched.
- "response_item" payloads, in both history modes (Legacy and Paginated):
  "message" (role, content[].text), "function_call" (name, arguments as a
  JSON string, call_id), "function_call_output" (call_id, output: a string or
  a list of {type, text}), "local_shell_call" (call_id, status,
  action.command list, action.working_directory), "custom_tool_call" (name,
  input, call_id) and "custom_tool_call_output" (call_id, output).
- Shell results carry an exit code only in their text: exec_command writes a
  header ("Process exited with code N", or "Process running with session ID
  N" while it still runs, a write_stdin call later reporting the code), older
  releases wrote {"output", "metadata": {"exit_code"}}. Paginated rollouts add
  "event_msg" item_completed records: a CommandExecution item (id = call_id,
  status, exit_code, aggregated_output cut to 64 KiB with a marker) and a
  FileChange item (id = call_id, status). Legacy rollouts add patch_apply_end
  (call_id, success, status). With no exit code in the log the event keeps
  exit_code None and a status, never a made-up exit code.
- apply_patch: the freeform tool (custom_tool_call "apply_patch"), the older
  function form ({"input": patch}) and a shell argv ["apply_patch", patch].
  "*** Add File" becomes a write, "*** Update File" an edit (one hunk) or a
  multi_edit (several), with context lines in both old and new text.
- Sub-agents: none read in this version (main transcript only).

Lines are read in binary, one at a time, capped at MAX_LINE bytes; a longer
line, a line that is not JSON, and a record type this reader does not know
are skipped and counted in Session.unrecognised. The reader starts no
process and makes no network call; paths it reads from the log are checked
later through donediff.shell.confined like every other transcript path.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..evidence import FAILURE_IN_OUTPUT, PASS_IN_OUTPUT, check_types, passed_counts
from ..models import EDIT, MULTI_EDIT, OTHER, SHELL, WRITE, Event, EventKind, Session
from ..parser import complete_background, epoch
from ..shell import is_remote_path
from .base import UnreadableSession

MAX_LINE = 32 * 1024 * 1024  # bytes in one log line; a longer line is skipped
_META_LIMIT = 4 * 1024 * 1024  # bytes of the first line read for discovery and detect
NEEDS_314 = "compressed session: needs Python 3.14 to read"

_FILE = re.compile(r"^rollout-[0-9A-Za-z_.:-]{1,200}\.jsonl(?:\.zst)?$")
_YEAR, _TWO = re.compile(r"^\d{4}$"), re.compile(r"^\d{2}$")
_TOP_TYPES = frozenset({
    "session_meta", "response_item", "event_msg", "turn_context", "compacted",
    "token_usage_record", "world_state", "retained_context", "security_risk_score",
    "dynamic_tools_updated", "inter_agent_communication",
    "inter_agent_communication_metadata", "realtime_item",
})
_RESPONSE_TYPES = frozenset({
    "additional_tools", "message", "agent_message", "reasoning", "local_shell_call",
    "function_call", "tool_search_call", "function_call_output", "tool_search_output",
    "custom_tool_call", "custom_tool_call_output", "web_search_call",
    "image_generation_call", "configuration_update", "compaction", "context_compaction",
})
_SHELL_FUNCTIONS = frozenset({"exec_command", "shell", "shell_command", "container.exec",
                              "local_shell"})
_PATCH_TOOLS = frozenset({"apply_patch", "applypatch"})
_ENDED = frozenset({"completed", "failed", "incomplete", "declined"})  # not "in_progress"
_WRAPPERS = frozenset({"bash", "sh", "zsh", "pwsh", "powershell", "cmd"})
_WRAPPER_FLAGS = frozenset({"-c", "-lc", "-ic", "-command", "/c"})
_HEADER_LINE = re.compile(
    r"^(?:Chunk ID: .*|Wall time: .*|Process exited with code -?\d+|"
    r"Process running with session ID \S+|Original token count: \d+|"
    r"Exit code: -?\d+|Total output lines: \d+)$")
_EXITED = re.compile(r"^(?:Process exited with code|Exit code:) (-?\d+)$", re.MULTILINE)
_RUNNING = re.compile(r"^Process running with session ID (\S+)$", re.MULTILINE)
_TRUNCATED = "... command output truncated for persistence ..."
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩]")


# -- where the sessions are ---------------------------------------------------

def codex_home() -> Path | None:
    """$CODEX_HOME when set (None unless it is a local folder), else ~/.codex."""
    env = os.environ.get("CODEX_HOME")
    if env:
        if is_remote_path(env):
            return None
        home = Path(env)
        return home if home.is_dir() else None
    return Path(os.path.expanduser("~")) / ".codex"


def sessions_dir() -> Path | None:
    home = codex_home()
    return home / "sessions" if home is not None else None


def _subdirs(path: Path, pattern: re.Pattern) -> list[Path]:
    """Child folders whose names match, never following a symlink or a Windows junction."""
    try:
        with os.scandir(path) as it:
            return sorted(Path(e.path) for e in it
                          if pattern.match(e.name) and e.is_dir(follow_symlinks=False)
                          and not getattr(e, "is_junction", lambda: False)())
    except OSError:
        return []


def _rollouts(day: Path) -> list[tuple[float, Path]]:
    found = []
    try:
        with os.scandir(day) as it:
            for entry in it:
                if _FILE.match(entry.name) and entry.is_file(follow_symlinks=False):
                    found.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
    except OSError:
        return []
    return found


def _stem(name: str) -> str:
    for suffix in (".jsonl.zst", ".jsonl"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def thread_id(name: str) -> str:
    """The session id in a rollout file name (after rollout-<19-char time>-)."""
    stem = _stem(name)
    core = stem[len("rollout-"):] if stem.startswith("rollout-") else stem
    return core[20:] if len(core) > 20 and core[19] == "-" else core


def is_compressed(path: Path) -> bool:
    return path.name.endswith(".zst")


def _open(path: Path):
    """The file opened for binary reading, decompressing a .zst one (3.14+)."""
    if is_compressed(path):
        if sys.version_info < (3, 14):
            raise UnreadableSession(NEEDS_314)
        from compression import zstd  # standard library on 3.14+
        return zstd.open(path, "rb")
    return open(path, "rb")


def _read_errors() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (OSError, EOFError)
    if sys.version_info >= (3, 14):
        from compression import zstd
        errors += (zstd.ZstdError,)
    return errors


def _lines(fh, limit: int):
    """Each line as bytes, or None for a line longer than `limit` (read past, not kept)."""
    while True:
        line = fh.readline(limit + 1)
        if not line:
            return
        if len(line) > limit and not line.endswith(b"\n"):
            while line and not line.endswith(b"\n"):
                line = fh.readline(limit + 1)
            yield None
            continue
        yield line


def session_meta(path: Path) -> dict | None:
    """The session_meta payload from the first record, or None."""
    try:
        with _open(path) as fh:
            for line in _lines(fh, _META_LIMIT):
                if line is None:
                    return None
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line.decode("utf-8", errors="replace"))
                break
            else:
                return None
    except (UnreadableSession, ValueError, *_read_errors()):
        return None
    if (isinstance(record, dict) and record.get("type") == "session_meta"
            and isinstance(record.get("payload"), dict)):
        return record["payload"]
    return None


def clean_path(value) -> str:
    """A path from the log as text: file:// URIs made paths, NUL or overlong refused."""
    if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
        return ""
    value = value.strip()
    if value.startswith("file://"):
        parts = urlsplit(value)
        path = unquote(parts.path)
        if "\x00" in path:
            return ""
        if parts.netloc:
            return "//" + parts.netloc + path  # a UNC share; confined() refuses it
        if re.match(r"^/[A-Za-z]:(?:/|$)", path):
            path = path[1:]
        return path
    return value


def _join(base: str, workdir: str) -> str:
    if not workdir:
        return base
    if workdir.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", workdir) or not base:
        return workdir
    return base.rstrip("/\\") + "/" + workdir


# -- what a record holds ------------------------------------------------------

def argv_command(argv) -> str:
    """A command line from an argv list, unwrapping one `bash -lc <script>`."""
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return ""
    if len(argv) == 3:
        head = re.split(r"[\\/]", argv[0])[-1].lower()
        head = re.sub(r"\.exe$", "", head)
        if head in _WRAPPERS and argv[1].lower() in _WRAPPER_FLAGS:
            return argv[2]
    return shlex.join(argv)


def output_text(output) -> tuple[str, bool | None]:
    """(text, success) of a function/custom tool output in any of its wire shapes."""
    if isinstance(output, str):
        return output, None
    if isinstance(output, list):
        texts = [str(i.get("text", "")) for i in output
                 if isinstance(i, dict) and isinstance(i.get("text"), str)]
        return "\n".join(texts), None
    if isinstance(output, dict):  # an older shape: {"content", "success"}
        content = output.get("content")
        success = output.get("success")
        return (content if isinstance(content, str) else "",
                success if isinstance(success, bool) else None)
    return "", None


def exec_result(text: str) -> tuple[int | None, str | None, str]:
    """(exit code, running session id, output body) from a shell tool's result text."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("output"), str):
            meta = data.get("metadata")
            code = meta.get("exit_code") if isinstance(meta, dict) else None
            return (code if isinstance(code, int) and not isinstance(code, bool) else None,
                    None, data["output"])
    head, sep, body = text.partition("Output:")
    header = [h for h in head.splitlines() if h.strip()]
    if not sep or not header or len(header) > 8 or not all(
            _HEADER_LINE.match(h.strip()) for h in header):
        return None, None, text
    head = "\n".join(h.strip() for h in header)
    body = body[1:] if body.startswith("\n") else body
    exited = _EXITED.search(head)
    running = _RUNNING.search(head)
    return (int(exited.group(1)) if exited else None,
            running.group(1) if running and not exited else None, body)


def parse_patch(text: str) -> list[tuple[str, str, object]]:
    """The file operations of an apply_patch body.

    ("add", path, content), ("update", path, [(old, new), ...]) with the
    path after any "*** Move to:", or ("delete", path, None).
    """
    ops: list[tuple[str, str, object]] = []
    lines = text.splitlines()
    if not any(line.strip() == "*** Begin Patch" for line in lines):
        return ops
    current: list | None = None
    hunk: tuple[list, list] | None = None

    def close_hunk():
        nonlocal hunk
        if hunk is not None and current is not None and (hunk[0] or hunk[1]):
            current[2].append(("\n".join(hunk[0]), "\n".join(hunk[1])))
        hunk = None

    for line in lines:
        if line.startswith("*** "):
            close_hunk()
            marker = line.rstrip()
            if marker.startswith("*** Add File: "):
                current = ["add", marker[14:].strip(), []]
                ops.append(current)
            elif marker.startswith("*** Update File: "):
                current = ["update", marker[17:].strip(), []]
                ops.append(current)
            elif marker.startswith("*** Delete File: "):
                ops.append(["delete", marker[17:].strip(), None])
                current = None
            elif marker.startswith("*** Move to: ") and current and current[0] == "update":
                current[1] = marker[13:].strip()
            elif marker == "*** End Patch":
                current = None
            continue
        if current is None:
            continue
        if current[0] == "add":
            if line.startswith("+"):
                current[2].append(line[1:])
            continue
        if line.startswith("@@"):
            close_hunk()
            hunk = ([], [])
            continue
        if hunk is None:
            hunk = ([], [])
        if line.startswith("-"):
            hunk[0].append(line[1:])
        elif line.startswith("+"):
            hunk[1].append(line[1:])
        elif line.startswith(" ") or line == "":
            hunk[0].append(line[1:])
            hunk[1].append(line[1:])
    close_hunk()
    result = []
    for op, path, data in ops:
        if op == "add":
            result.append((op, path, "\n".join(data) + ("\n" if data else "")))
        else:
            result.append((op, path, data))
    return result


# -- the reader ---------------------------------------------------------------

class _Parse:
    """One pass over a rollout, building the Session."""

    def __init__(self, path: Path):
        self.session = Session(path=str(path))
        self.index = 0
        self.line_no = 0
        self.clock = float("-inf")
        self.timestamp = ""
        self.cwd = ""
        self.pending: dict[str, list[Event]] = {}  # call_id -> events awaiting output
        self.calls: dict[str, list[Event]] = {}  # call_id -> its events, kept
        self.stdin: dict[str, str] = {}  # write_stdin call_id -> process session id
        self.running: dict[str, tuple[Event, frozenset]] = {}  # process session id -> launch

    # events

    def _event(self, name: str, action: str, call_id: str, tool_input: dict,
               cwd: str = "") -> Event:
        event = Event(kind=EventKind.TOOL_CALL, index=self.index, timestamp=self.timestamp,
                      tool_name=name, action=action, tool_id=call_id,
                      tool_input=tool_input, t=self.clock, cwd=cwd or self.cwd,
                      result_seen=False)
        if action == SHELL:
            event.check_types = check_types(event.command)
        self.session.events.append(event)
        self.index += 1
        return event

    def _track(self, call_id: str, events: list[Event]) -> None:
        if call_id:
            self.pending[call_id] = events
            self.calls[call_id] = events

    def shell(self, name: str, call_id: str, command: str, workdir: str = "",
              status: str = "") -> None:
        # A heredoc patch through the shell (`apply_patch <<EOF`) stays a shell command.
        event = self._event(name, SHELL, call_id, {"command": command},
                            _join(self.cwd, clean_path(workdir)))
        event.status = status
        if status in _ENDED:  # the call record itself says how the run ended
            event.result_seen = True
        self._track(call_id, [event])

    def patch(self, name: str, call_id: str, text: str) -> None:
        events = []
        for op, path, data in parse_patch(text):
            if op == "add":
                events.append(self._event(name, WRITE, call_id,
                                          {"file_path": path, "content": data}))
            elif op == "update":
                hunks = data or [("", "")]
                if len(hunks) == 1:
                    tool_input = {"file_path": path, "old_string": hunks[0][0],
                                  "new_string": hunks[0][1]}
                    events.append(self._event(name, EDIT, call_id, tool_input))
                else:
                    edits = [{"old_string": o, "new_string": n} for o, n in hunks]
                    events.append(self._event(name, MULTI_EDIT, call_id,
                                              {"file_path": path, "edits": edits}))
            else:
                events.append(self._event(name, OTHER, call_id, {"file_path": path}))
        if not events:
            events.append(self._event(name, OTHER, call_id, {}))
        self._track(call_id, events)

    def other(self, name: str, call_id: str, tool_input: dict) -> None:
        self._track(call_id, [self._event(name, OTHER, call_id, tool_input)])

    # results

    @staticmethod
    def _hints(event: Event) -> None:
        event.failure_hint = bool(FAILURE_IN_OUTPUT.search(event.output))
        event.pass_hint = bool(PASS_IN_OUTPUT.search(event.output))
        event.passed_counts = passed_counts(event.output) if event.check_types else frozenset()
        event.output_truncated = _TRUNCATED in event.output

    def _finish(self, event: Event, code: int) -> None:
        """A run that went on in the background ends with this exit code."""
        types = next((t for e, t in self.running.values() if e is event), frozenset())
        for key in [k for k, (e, _) in self.running.items() if e is event]:
            del self.running[key]
        complete_background(event, code, self.clock, self.timestamp)
        event.done_order = self.index - 0.5
        event.check_types = types
        event.status = "completed" if code == 0 else "failed"
        self._hints(event)

    def output(self, call_id: str, raw) -> None:
        events = self.pending.pop(call_id, None)
        if not events:
            return
        text, success = output_text(raw)
        for event in events:
            event.result_seen = True
            if event.tool_action == SHELL:
                self._shell_output(event, text, success)
            else:
                event.output = text
                if success is False or "verification failed" in text[:200]:
                    event.is_error = True
                if event.tool_name == "write_stdin":
                    self._stdin_output(call_id, text)

    def _shell_output(self, event: Event, text: str, success: bool | None) -> None:
        code, running, body = exec_result(text)
        event.output = body
        if code is not None:
            event.exit_code = code
            event.is_error = code != 0
            event.status = "completed" if code == 0 else "failed"
        elif running is not None:
            event.background = True
            self.running[running] = (event, event.check_types)
            event.check_types = frozenset()
            event.status = "running"
        else:
            event.is_error = success is False
            event.status = event.status or ("failed" if success is False else "not recorded")
        self._hints(event)

    def _stdin_output(self, call_id: str, text: str) -> None:
        code, _, body = exec_result(text)
        launch = self.running.get(self.stdin.get(call_id, ""))
        if code is None or launch is None:
            return
        event = launch[0]
        event.output = (event.output + "\n" + body) if event.output else body
        self._finish(event, code)

    def item_completed(self, item: dict) -> None:
        kind = item.get("type")
        events = self.calls.get(str(item.get("id", "")))
        if not events:
            return
        status = item.get("status") if isinstance(item.get("status"), str) else ""
        if kind == "FileChange":
            if status in ("failed", "declined"):
                for event in events:
                    event.is_error = True
            return
        if kind != "CommandExecution" or events[0].tool_action != SHELL:
            return
        event = events[0]
        code = item.get("exit_code")
        code = code if isinstance(code, int) and not isinstance(code, bool) else None
        aggregated = item.get("aggregated_output")
        running = any(e is event for e, _ in self.running.values())
        if event.background and not running:
            return  # write_stdin already saw its exit
        event.result_seen = True
        if isinstance(aggregated, str):
            event.output = aggregated
        if event.background:
            if code is not None:  # the item ends the run
                self._finish(event, code)
            return
        if code is not None:
            event.exit_code = code
            event.is_error = code != 0
            event.status = "completed" if code == 0 else "failed"
        elif status:
            event.status = status
            event.is_error = event.is_error or status in ("failed", "declined")
        self._hints(event)

    def patch_end(self, payload: dict) -> None:
        events = self.calls.get(str(payload.get("call_id", "")))
        if events and (payload.get("success") is False
                       or payload.get("status") in ("failed", "declined")):
            for event in events:
                event.is_error = True

    # records

    def record(self, record: dict) -> None:
        rtype = record.get("type")
        payload = record.get("payload")
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            self.timestamp = timestamp
            session = self.session
            session.first_timestamp = session.first_timestamp or timestamp
            session.last_timestamp = timestamp
            seconds = epoch(timestamp)
            if seconds is not None and seconds > self.clock:
                self.clock = seconds
        if rtype not in _TOP_TYPES or not isinstance(payload, dict):
            self.session.unrecognised += 1
            return
        if rtype == "session_meta":
            self.meta(payload)
        elif rtype == "turn_context":
            cwd = clean_path(payload.get("cwd"))
            if cwd:
                self.cwd = cwd
        elif rtype == "response_item":
            self.response(payload)
        elif rtype == "event_msg":
            if payload.get("type") == "item_completed" and isinstance(payload.get("item"), dict):
                self.item_completed(payload["item"])
            elif payload.get("type") == "patch_apply_end":
                self.patch_end(payload)

    def meta(self, payload: dict) -> None:
        session = self.session
        if session.session_id:
            return
        session.session_id = str(payload.get("id") or payload.get("session_id") or "")[:128]
        session.cwd = clean_path(payload.get("cwd"))
        session.version = str(payload.get("cli_version", ""))[:64]
        git = payload.get("git")
        branch = git.get("branch") if isinstance(git, dict) else None
        session.git_branch = branch[:256] if isinstance(branch, str) else ""
        session.slug = session.session_id[:12]
        self.cwd = self.cwd or session.cwd

    def response(self, payload: dict) -> None:
        ptype = payload.get("type")
        if ptype not in _RESPONSE_TYPES:
            self.session.unrecognised += 1
            return
        call_id = payload.get("call_id")
        call_id = call_id if isinstance(call_id, str) else ""
        name = payload.get("name") if isinstance(payload.get("name"), str) else ""
        if ptype == "message":
            self.message(payload)
        elif ptype == "function_call":
            raw = payload.get("arguments")
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                args = None
            args = args if isinstance(args, dict) else {}
            self.function_call(name, call_id, args)
        elif ptype == "custom_tool_call":
            text = payload.get("input") if isinstance(payload.get("input"), str) else ""
            if name in _PATCH_TOOLS:
                self.patch(name, call_id, text)
            else:
                self.other(name, call_id, {"input": text})
        elif ptype == "local_shell_call":
            action = payload.get("action")
            action = action if isinstance(action, dict) else {}
            status = payload.get("status") if isinstance(payload.get("status"), str) else ""
            argv = action.get("command")
            if isinstance(argv, list) and len(argv) == 2 and argv[0] in _PATCH_TOOLS:
                self.patch("local_shell", call_id, str(argv[1]))
            else:
                workdir = action.get("working_directory")
                self.shell("local_shell", call_id, argv_command(argv),
                           workdir if isinstance(workdir, str) else "", status)
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            self.output(call_id, payload.get("output"))

    def function_call(self, name: str, call_id: str, args: dict) -> None:
        if name in _SHELL_FUNCTIONS:
            command = args.get("cmd")
            if not isinstance(command, str):
                command = args.get("command")
            argv = command if isinstance(command, list) else None
            if argv is not None and len(argv) == 2 and argv[0] in _PATCH_TOOLS:
                self.patch(name, call_id, str(argv[1]))
                return
            command = argv_command(argv) if argv is not None else (
                command if isinstance(command, str) else "")
            workdir = args.get("workdir")
            self.shell(name, call_id, command, workdir if isinstance(workdir, str) else "")
        elif name in _PATCH_TOOLS:
            text = args.get("input")
            self.patch(name, call_id, text if isinstance(text, str) else "")
        else:
            if name == "write_stdin" and call_id:
                self.stdin[call_id] = str(args.get("session_id", ""))
            self.other(name, call_id, args)

    def message(self, payload: dict) -> None:
        role = payload.get("role")
        content = payload.get("content")
        texts = [str(i.get("text")) for i in content
                 if isinstance(i, dict) and isinstance(i.get("text"), str)] \
            if isinstance(content, list) else []
        if role == "assistant":
            for text in texts:
                text = text.strip()
                if text:
                    self.session.events.append(Event(
                        kind=EventKind.TEXT, index=self.index, timestamp=self.timestamp,
                        text=text, t=self.clock))
                    self.index += 1
        elif role == "user" and texts:
            first = texts[0].lstrip()
            if first and not first.startswith(("<", "# AGENTS.md")):
                self.session.prompt_starts.append(self.index)
                self.session.prompt_marks.append((self.index, self.line_no))


def parse_rollout(path: str | Path) -> Session:
    path = Path(path)
    state = _Parse(path)
    errors = _read_errors()
    with _open(path) as fh:
        try:
            for line in _lines(fh, MAX_LINE):
                if line is None:
                    state.session.unrecognised += 1
                    continue
                line = line.strip()
                if not line:
                    continue
                state.line_no += 1
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    state.session.unrecognised += 1
                    continue
                if not isinstance(record, dict):
                    state.session.unrecognised += 1
                    continue
                state.record(record)
        except errors:
            state.session.unrecognised += 1  # a cut-off or damaged compressed file
    return state.session


class CodexReader:
    name = "codex"

    @property
    def location(self) -> str:
        return "$CODEX_HOME/sessions" if os.environ.get("CODEX_HOME") else "~/.codex/sessions"

    def discover(self, project_filter: str | None = None) -> list[Path]:
        root = sessions_dir()
        if root is None or not root.is_dir():
            return []
        found: list[tuple[float, Path]] = []
        for year in _subdirs(root, _YEAR):
            for month in _subdirs(year, _TWO):
                for day in _subdirs(month, _TWO):
                    found.extend(_rollouts(day))
        found.sort(key=lambda pair: pair[0], reverse=True)
        paths = [p for _, p in found]
        if project_filter:
            wanted = project_filter.lower()
            paths = [p for p in paths if wanted in self._cwd(p).lower()]
        return paths

    @staticmethod
    def _cwd(path: Path) -> str:
        meta = session_meta(path)
        return clean_path(meta.get("cwd")) if meta else ""

    def resolve(self, target: str, project_filter: str | None = None) -> Path:
        direct = Path(target)
        if direct.is_file():
            return direct
        sessions = self.discover(project_filter)
        if target == "latest":
            if not sessions:
                raise FileNotFoundError(f"no transcripts found under {self.location}")
            return sessions[0]
        for candidate in sessions:
            if (thread_id(candidate.name).startswith(target)
                    or _stem(candidate.name).startswith(target)):
                return candidate
        raise FileNotFoundError(f"no transcript matching {target!r}")

    def detect(self, path: Path) -> bool:
        path = Path(path)
        if is_compressed(path):
            return bool(_FILE.match(path.name))
        return session_meta(path) is not None

    def describe(self, path: Path) -> tuple[str, str]:
        short = thread_id(path.name)[:8]
        if is_compressed(path) and sys.version_info < (3, 14):
            return short, "(compressed)"
        cwd = self._cwd(path)
        project = re.split(r"[\\/]", cwd.rstrip("\\/"))[-1] if cwd else "?"
        return short, _UNSAFE.sub("?", project)[:60]

    def parse(self, path: Path) -> Session:
        return parse_rollout(path)

    def attach_sources(self, session: Session) -> Session:
        return session  # no sub-agent transcripts are read for Codex yet


CODEX = CodexReader()
