"""Split a shell command into statements and pipeline stages.

The auditor needs to know what a command actually ran, not which words
appear in it: `grep -rn pytest src | head` reads files, it does not run
the tests. This module splits a Bash or PowerShell command line into
statements (separated by &&, ||, ;, & or a newline) and each statement
into pipeline stages (separated by |), outside quotes and here-doc
bodies, and normalises the head of each stage so a check runner can be
recognised by the program it starts.

It is a small, linear scanner, not a shell. It errs toward not seeing a
check (a command it cannot read counts as no evidence and no signal).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Programs whose job is to page or filter output. As the last stage of a
# pipeline they decide its exit status (without pipefail), so a failing
# test run piped into one of them reports exit 0.
FILTERS = frozenset({
    "tail", "head", "grep", "egrep", "fgrep", "rg", "less", "more",
    "sort", "uniq", "wc", "cut", "awk", "sed", "tee", "tr", "column",
    "select-object", "select", "select-string", "sls", "findstr",
    "out-string", "out-host", "where-object", "where", "measure-object",
})

# Words that only modify how the next program starts.
_PREFIX_WORDS = frozenset({
    "do", "then", "else", "elif", "time", "sudo", "env", "exec", "nohup",
    "command", "builtin", "!", "&", "{", "(", "nice", "xvfb-run",
})
# Runners that start the program named after them.
_RUNNERS = {
    "uv": "run", "poetry": "run", "pipenv": "run", "hatch": "run",
    "pdm": "run", "rye": "run",
}
_SHELL_WRAPPERS = frozenset({"bash", "sh", "zsh", "pwsh", "powershell", "cmd"})
_WRAPPER_FLAGS = frozenset({"-c", "-lc", "-ic", "-command", "/c", "/k"})

_WORD = re.compile(r"""(?:"(?:[^"\\]|\\.)*"?|'[^']*'?|[^\s"'])+""")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PY_HEAD = re.compile(r"(?:python[0-9.]*|py)", re.IGNORECASE)
_EXE_SUFFIX = re.compile(r"\.(?:exe|cmd|bat)$", re.IGNORECASE)
_HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
# Interpreter options that may sit between python (or py) and -m.
_PY_VERSION_FLAG = re.compile(r"^-(?:[0-9][0-9.]*(?:-(?:32|64))?|V:\S+)$")
_PY_SINGLE_FLAGS = re.compile(r"^-[uBOEIsSbdqRP]+$")
_PY_ARG_FLAGS = frozenset({"-X", "-W"})
# timeout's options that take a value, before its duration.
_TIMEOUT_ARG_FLAGS = frozenset({"-s", "-k", "--signal", "--kill-after"})


@dataclass
class Statement:
    """One pipeline and the operator that follows it ('' at the end)."""

    stages: list[str]
    op: str
    start: int = 0  # span of the statement in the command, operator excluded
    end: int = 0


def split_statements(command: str) -> list[Statement]:
    """Split at top-level &&, ||, ;, &, newline and |, in one pass."""
    statements: list[Statement] = []
    stages: list[str] = []
    buf: list[str] = []
    heredocs: list[tuple[str, bool]] = []
    started = False  # the current stage has a non-blank character
    stmt_start = 0
    i, n = 0, len(command)

    def end_stage() -> None:
        nonlocal started
        stages.append("".join(buf).strip())
        buf.clear()
        started = False

    def end_statement(op: str, at: int) -> None:
        nonlocal stages, stmt_start
        end_stage()
        if any(stages):
            statements.append(Statement(stages=stages, op=op,
                                        start=stmt_start, end=at))
        stages = []
        stmt_start = at + len(op)

    while i < n:
        ch = command[i]
        if ch in "\\'\"@<":
            started = True
        if ch == "\\" and i + 1 < n:
            buf.append(command[i:i + 2])
            i += 2
            continue
        if ch == "'":
            close = command.find("'", i + 1)
            close = n - 1 if close < 0 else close
            buf.append(command[i:close + 1])
            i = close + 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and command[j] != '"':
                j += 2 if command[j] == "\\" else 1
            buf.append(command[i:j + 1])
            i = j + 1
            continue
        if ch == "@" and command[i + 1:i + 2] in ("'", '"') \
                and command[i + 2:i + 3] in ("\n", "\r"):
            # PowerShell here-string: @' ... '@ on its own line.
            closer = "\n" + command[i + 1] + "@"
            close = command.find(closer, i + 2)
            close = n if close < 0 else close + len(closer)
            buf.append(command[i:close])
            i = close
            continue
        if ch == "<" and command.startswith("<<", i) and not command.startswith("<<<", i):
            match = _HEREDOC.match(command, i)
            if match:
                heredocs.append((match.group(3), match.group(1) == "-"))
                buf.append(match.group())
                i = match.end()
                continue
        if ch == "\n":
            end_statement("\n", i)
            i += 1
            # Skip here-doc bodies opened on the line that just ended.
            for word, strip_tabs in heredocs:
                while i < n:
                    eol = command.find("\n", i)
                    eol = n if eol < 0 else eol
                    line = command[i:eol].rstrip("\r")
                    i = eol + 1
                    if (line.lstrip("\t") if strip_tabs else line) == word:
                        break
            heredocs.clear()
            continue
        if ch == "#" and not started:
            # A comment at the start of a stage runs to the end of the line.
            eol = command.find("\n", i)
            i = n if eol < 0 else eol
            continue
        two = command[i:i + 2]
        if two in ("&&", "||"):
            end_statement(two, i)
            i += 2
            continue
        if ch == ";":
            end_statement(";", i)
            i += 1
            continue
        if ch == "|":
            end_stage()
            i += 2 if two == "|&" else 1
            continue
        if ch == "&" and command[i + 1:i + 2] != ">" and command[i - 1:i] not in (">", "<"):
            end_statement("&", i)
            i += 1
            continue
        buf.append(ch)
        if not ch.isspace():
            started = True
        i += 1
    end_statement("", n)
    return statements


def _unquote(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'":
        return word[1:-1]
    return word.strip("\"'")


def _basename(word: str) -> str:
    word = _unquote(word)
    cut = max(word.rfind("/"), word.rfind("\\"))
    return word[cut + 1:] if cut >= 0 else word


def _skip_timeout(words: list[str]) -> list[str]:
    """Drop `timeout`, its options and its duration: `timeout -s KILL 60 x` is x."""
    i = 1
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += 2 if words[i] in _TIMEOUT_ARG_FLAGS else 1
    if i < len(words) and words[i] == "--":
        i += 1
    return words[i + 1:]


def _skip_python_options(rest: list[str]) -> list[str]:
    """Drop interpreter options before -m: `-3 -X utf8 -u -m pytest` is `-m pytest`."""
    i = 0
    while i < len(rest) and rest[i] != "-m":
        word = rest[i]
        if word in _PY_ARG_FLAGS:
            i += 2
        elif _PY_VERSION_FLAG.match(word) or _PY_SINGLE_FLAGS.match(word):
            i += 1
        else:
            return rest
    return rest[i:] if i < len(rest) else rest


def _drop_prefixes(words: list[str]) -> list[str]:
    """The words after the ones that only change how the program starts:
    env assignments, sudo, time and the like, timeout and its options, and
    runners such as `uv run`."""
    while words:
        low = words[0].lower()
        if low in _PREFIX_WORDS or _ASSIGNMENT.match(words[0]):
            words = words[1:]
        elif low == "timeout" and len(words) > 1:
            words = _skip_timeout(words)
        elif low in _RUNNERS and len(words) > 1 and words[1] == _RUNNERS[low]:
            words = words[2:]
        else:
            break
    return words


def normalize_stage(stage: str, _depth: int = 0) -> str:
    """The stage with its prefixes dropped and its program reduced to a name.

    `FOO=1 sudo "C:/Python314/python.exe" -m pytest -q` becomes
    `python -m pytest -q`. A shell wrapper (`bash -c "..."`) returns the
    normalised first stage of its inner command.
    """
    words = _drop_prefixes(_WORD.findall(stage.strip().lstrip("({!& \t")))
    if not words:
        return ""
    head = _EXE_SUFFIX.sub("", _basename(words[0])).lower()
    if _PY_HEAD.fullmatch(head):
        head = "python"
    rest = words[1:]
    if head == "python":
        rest = _skip_python_options(rest)
    if head == "git":
        while len(rest) >= 2 and rest[0] in ("-C", "-c"):
            rest = rest[2:]
    if (head in _SHELL_WRAPPERS and rest and rest[0].lower() in _WRAPPER_FLAGS
            and _depth < 2):
        inner = _unquote(" ".join(rest[1:]))
        statements = split_statements(inner)
        if statements and statements[0].stages:
            return normalize_stage(statements[0].stages[0], _depth + 1)
        return ""
    return " ".join([head] + rest)


# Programs that start the command after their own words. Masking reads a
# shell started through them too (SEC-251); a named command's own status
# does not pass through xargs, so named_status keeps the plain form.
_TIMEOUTS = frozenset({"timeout", "gtimeout"})
_LAUNCHER_ARG_FLAGS = {
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a", "--max-args",
                        "--max-procs", "--max-lines", "--delimiter", "--arg-file", "--eof",
                        "--max-chars"}),
}


def _skip_launcher(words: list[str], arg_flags: frozenset[str]) -> list[str]:
    """Drop a launcher, its options and their values: `xargs -n 1 sh -c x` is `sh -c x`."""
    i = 1
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += 2 if words[i] in arg_flags else 1
    if i < len(words) and words[i] == "--":
        i += 1
    return words[i:]


def wrapper_inner(stage: str, launchers: bool = False) -> str | None:
    """The command a `bash -c "..."` or `cmd /c "..."` stage runs, else None.
    With `launchers`, the shell may also be started through `timeout` or
    `gtimeout` and its duration, or `stdbuf` or `xargs` and their options."""
    words = _WORD.findall(stage.strip().lstrip("({!& \t"))
    while words:
        name = _EXE_SUFFIX.sub("", _basename(words[0])).lower()
        if words[0].lower() in _PREFIX_WORDS or _ASSIGNMENT.match(words[0]):
            words = words[1:]
        elif launchers and name in _TIMEOUTS and len(words) > 1:
            words = _skip_timeout(words)
        elif launchers and name in _LAUNCHER_ARG_FLAGS:
            words = _skip_launcher(words, _LAUNCHER_ARG_FLAGS[name])
        else:
            break
    if len(words) < 3:
        return None
    head = _EXE_SUFFIX.sub("", _basename(words[0])).lower()
    if head in _SHELL_WRAPPERS and words[1].lower() in _WRAPPER_FLAGS:
        return _unquote(" ".join(words[2:]))
    return None


def program(stage: str) -> str:
    """The program name a stage starts, lowercased."""
    norm = normalize_stage(stage)
    return norm.split(" ", 1)[0] if norm else ""


# -- a named command and its own exit status (claim type COMMAND_OK) ---------

# Programs that run the script named as their first argument: `python hello.py`
# runs `hello.py` (python3 and py count as python).
_SCRIPT_RUNNERS = frozenset({"python", "node", "bun", "bash", "sh", "zsh", "pwsh",
                             "powershell", "ruby", "perl", "php"})


def command_words(text: str) -> list[str]:
    """The words of a command as this scanner splits them, quotes removed."""
    return [_unquote(w) for w in _WORD.findall(text)]


def starts_command(stage: str, words: list[str]) -> bool:
    """True when the stage runs the command `words`: its own words begin with
    them, as written or after prefixes (`FOO=1`, `sudo`, `uv run`), or `words`
    is one script that an interpreter runs as its first argument (`python
    hello.py` runs `hello.py`). Words compare exactly: `./hello.py` is not
    `hello.py`, `python3` is not `python`, and `cat hello.py` runs no script."""
    if not words or not all(words):
        return False
    raw = _WORD.findall(stage.strip().lstrip("({!& \t"))
    bare = [_unquote(w) for w in _drop_prefixes(raw)]
    if [_unquote(w) for w in raw][:len(words)] == words or bare[:len(words)] == words:
        return True
    if len(words) == 1 and len(bare) > 1 and bare[1] == words[0]:
        head = _EXE_SUFFIX.sub("", _basename(bare[0])).lower()
        return head in _SCRIPT_RUNNERS or bool(_PY_HEAD.fullmatch(head))
    return False


def named_status(command: str, is_target, _depth: int = 0) -> tuple[bool, bool, str] | None:
    """Whether the exit status of `command` is that of its last stage that
    `is_target` accepts: None when no stage is accepted, else (success,
    failure, how).

    success: exit status 0 means that stage ran and exited 0. The stage ends
    its pipeline (a pipe hands the status on), is not negated (`! x`), does not
    follow `||` (it ran only if the left side failed), and only `&&` comes
    after it (`; x`, `|| x` and `& x` hand the status on). `how` names what
    breaks this, in the gaming signals' words (`| tail`, `; echo`).
    failure: a non-zero status can only be that stage's: success holds, the
    stage runs unconditionally (first, or after `;`, a newline or `&`), and
    nothing comes after it. A `bash -c "..."` stage is read through.
    """
    statements = [st for st in split_statements(command) if any(st.stages)]
    hit = None
    for k, statement in enumerate(statements):
        stages = [s for s in statement.stages if s]
        for i, stage in enumerate(stages):
            if is_target(stage):
                inner = (True, True, "")
            else:
                wrapped = wrapper_inner(stage) if _depth < 2 else None
                inner = named_status(wrapped, is_target, _depth + 1) if wrapped else None
                if inner is None:
                    continue
            hit = (k, i, stages, inner)
    if hit is None:
        return None
    k, i, stages, (_, inner_failure, how) = hit
    before = statements[k - 1].op if k else ""
    if i < len(stages) - 1:
        how = "| " + (program(stages[i + 1]) or "?")
    elif stages[0].lstrip("({ \t").startswith("!"):
        how = "! (status negated)"
    elif before == "||":
        how = (program(statements[k - 1].stages[0]) or "?") + " ||"
    for j in range(k, len(statements)):
        op, last = statements[j].op, j == len(statements) - 1
        if how or (op == "&&" and not last) or (last and op in ("", ";", "\n")):
            continue
        nxt = "" if last else program(statements[j + 1].stages[0])
        how = (("; " if op in (";", "\n") else op + " ")
               + (nxt or ("(background)" if op == "&" else "?")))
    success = not how
    failure = (success and inner_failure and k == len(statements) - 1
               and before in ("", ";", "\n", "&"))
    return success, failure, how


_CD_COMMANDS = frozenset({"cd", "chdir", "pushd", "set-location", "sl"})
_DRIVE_MSYS = re.compile(r"^/([A-Za-z])(?=/|$)")
_DRIVE = re.compile(r"^[A-Za-z]:/")


def is_remote_path(path) -> bool:
    """UNC, device or NT-namespace path (\\\\host\\share, //host/share, \\??\\...).

    Paths come from the transcript, so they are untrusted. On Windows,
    stat-ing \\\\host\\share opens an SMB connection to that host and sends
    the user's NTLM credentials, so these are never touched.
    """
    text = str(path).replace("/", "\\")
    return text.startswith("\\\\") or text.startswith("\\??\\")


_AUTOMOUNT = ("/net", "/Network")  # POSIX folders where a path names a remote host
MAX_LINK_HOPS = 40
_LOCAL_DRIVE = re.compile(r"[A-Za-z]:\\")


def _remote_form(path) -> bool:
    """is_remote_path, plus the POSIX automount folders (/net/<host>, /Network/...)."""
    if is_remote_path(path):
        return True
    if os.name == "nt":
        return False
    text = str(path)
    return any(text == top or text.startswith(top + "/") for top in _AUTOMOUNT)


def _links_reach_network(path: Path) -> bool:
    """A link on the way to `path` (or a link it leads to) names a remote path.

    Walks the path one part at a time with lstat and readlink only, splicing
    each link's target in, so no link is followed before its target is read.
    A link that cannot be read, or too many links, counts as remote.
    """
    pending = list(Path(os.path.abspath(path)).parts)
    if not pending:
        return False
    current = Path(pending.pop(0))
    hops = 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent  # `current` holds no link, so this is exact
            continue
        step = current / part
        try:
            if not os.path.islink(step):
                current = step
                continue
            target = os.readlink(step)
        except (OSError, ValueError):
            return True
        # Windows returns a local target as \\?\C:\...; \\?\UNC\host\... stays remote.
        if target[:4] in ("\\\\?\\", "\\??\\") and _LOCAL_DRIVE.match(target[4:]):
            target = target[4:]
        hops += 1
        if hops > MAX_LINK_HOPS or _remote_form(target):
            return True
        target_path = Path(target) if os.path.isabs(target) else current / target
        if _remote_form(target_path):
            return True
        parts = list(target_path.parts)
        current, pending = Path(parts[0]), parts[1:] + pending
    return False


def confined(path, root) -> Path | None:
    """`path` resolved (symlinks followed) when it lands inside `root`, else None.

    Both come from the transcript, so both are untrusted. Network and device
    paths (and on POSIX /net/... and /Network/...), and links on the way whose
    target is one, are refused before anything follows them; a relative path is joined
    to `root`; `..` parts and symlinks that lead outside `root` are refused.
    Every disk or git check of a path named in a transcript goes through here.
    """
    if not path or not root or _remote_form(path) or _remote_form(root):
        return None
    try:
        base = Path(os.path.normpath(windows_drive(str(root))))
        if _links_reach_network(base):
            return None
        base = base.resolve()
        full = Path(windows_drive(str(path).strip().strip("\"'")))
        full = full if full.is_absolute() else base / full
        full = Path(os.path.normpath(full))
        # Links are read, never followed, until every one on the way is known to
        # stay local: resolve() would touch a link's network target first.
        if _remote_form(full) or _links_reach_network(full):
            return None
        resolved = full.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if is_remote_path(resolved):
        return None
    return resolved if resolved == base or resolved.is_relative_to(base) else None


def base_name(path) -> str:
    """The file name of a path from the log, split at / and \\ on every OS.

    A transcript written on Windows names C:\\proj\\a.py; audited on Linux or
    macOS, Path(...).name would return the whole string, so the same log
    would get a different verdict there.
    """
    return PurePosixPath(str(path).replace("\\", "/")).name


def windows_drive(path: str) -> str:
    """/c/Users/x (Git Bash form) as C:/Users/x on Windows; else unchanged."""
    return _DRIVE_MSYS.sub(lambda m: m.group(1) + ":", path) if os.name == "nt" else path


def norm_path(path: str, base: str | None = None) -> str | None:
    """A comparable form of a path: forward slashes, lowercase, no dot parts.

    /c/Users and C:\\Users are the same folder. A relative path is joined
    to `base`; without one, or with a variable or ~ in it, None.
    """
    path = _unquote(path.strip()).replace("\\", "/")
    if not path or "$" in path or path.startswith("~") or "%" in path:
        return None
    path = _DRIVE_MSYS.sub(lambda m: m.group(1) + ":", path)
    if not (_DRIVE.match(path) or path.startswith("/")):
        if base is None:
            return None
        path = base + "/" + path
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if len(parts) > 1:
                parts.pop()
            continue
        parts.append(part)
    joined = "/".join(parts).lower()
    return joined if _DRIVE.match(joined + "/") else "/" + joined


def run_directory(command: str, cwd: str, is_target) -> str | None:
    """The folder the first stage matching `is_target` runs in, or None.

    Follows `cd`, `pushd` and `Set-Location` from the call's working
    directory, in order. None when that cannot be told (no known start,
    a variable in the path, or no matching stage).
    """
    current = norm_path(cwd) if cwd else None
    for statement in split_statements(command):
        for stage in statement.stages:
            if not stage:
                continue
            if is_target(stage):
                return current
            words = _WORD.findall(normalize_stage(stage))
            if words and words[0] in _CD_COMMANDS:
                args = [w for w in words[1:] if not w.startswith("-")
                        and w.lower() != "/d"]
                current = norm_path(args[-1], current) if args else None
    return None
