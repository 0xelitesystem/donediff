"""Codex "code mode": the one `exec` tool whose input is JavaScript.

Format notes (read from github.com/openai/codex at tag rust-v0.169.0,
commit d32063f96c2d; the format is internal and moves):

- The model calls one freeform tool, `exec` (code-mode-protocol/src/lib.rs:61),
  logged as a custom_tool_call whose `input` is JavaScript source. The script
  calls nested tools as `await tools.<name>(input)`, where input is one string
  (freeform tools such as apply_patch) or one object (function tools such as
  exec_command {cmd, workdir}) (code-mode-protocol/src/description.rs:24-29,
  core/src/tools/code_mode/mod.rs:427-470). A rejected nested call throws in
  the script (core/src/tools/code_mode/mod.rs:425, `result.await?`).
- The exec result is a custom_tool_call_output whose `output` is a LIST of
  {type: input_text, text} parts. Codex puts its own header in part 0:
  "<status>\\nWall time <s> seconds[ (code-mode ...)]\\nOutput:\\n"
  (core/src/tools/code_mode/output.rs:28-33 and :62-66), status being
  "Script completed", "Script failed", "Script terminated" or "Script running
  with cell ID <id>" (core/src/tools/code_mode/mod.rs:311-325). The parts after
  it are whatever the script printed with text(); a failed script may end
  with "Script error:\\n<text>" (mod.rs:296-300).
- There is NO per-call exit code in that format: exec_command hands the
  script an object with exit_code (core/src/tools/context.rs:493-518), and
  the script decides what to print. So a nested shell run gets a status only,
  never an exit code, and printed text never decides an outcome.
- `notify(value)` adds extra custom_tool_call_output records for the same
  call, as a plain string (core/src/tools/code_mode/delegate.rs:464-488):
  only a list whose first part is the header above is the exec's result.
- A script still running yields "Script running with cell ID <id>"; a later
  function_call `wait` {cell_id} returns the final result in the same format
  (code-mode-protocol/src/lib.rs:62, core/src/tools/code_mode/wait_handler.rs:118-164).

The scanner here is conservative: no eval, no JS engine. It tokenizes the
source (strings, template literals, comments and regex literals skipped) and
reads only `tools.<name>(<literal>)` calls whose one argument is a plain
string, number, boolean, null, or an array/object of those. Every other use
of `tools` (a computed name, an argument built from variables, an alias) is
counted as unparsed and becomes no event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EXEC_TOOL = "exec"  # code-mode-protocol/src/lib.rs:61
WAIT_TOOL = "wait"  # code-mode-protocol/src/lib.rs:62

MAX_SOURCE = 4 * 1024 * 1024  # characters of one exec input scanned; longer is unparsed
MAX_DEPTH = 32  # nesting of object/array literals in one argument
MAX_CALLS = 1000  # nested calls read from one exec; the rest are counted

NO_ARG = object()  # `tools.x()` with no argument

# core/src/tools/code_mode/output.rs:31 and :64, mod.rs:311-325
_HEADER = re.compile(
    r"\A(Script completed|Script failed|Script terminated|"
    r"Script running with cell ID (\S{1,128}))\n"
    r"Wall time -?\d{1,12}(?:\.\d{1,6})? seconds(?: \(code-mode [^\n]{0,200}\))?\n"
    r"Output:\n\Z")

COMPLETED, FAILED, TERMINATED, RUNNING = "completed", "script failed", "script terminated", "running"
_STATES = {"Script completed": COMPLETED, "Script failed": FAILED,
           "Script terminated": TERMINATED}

# Words and operators after which a call in the script may not have run, or
# may have failed without failing the script: with any of them a completed
# script is no proof that each call in it succeeded.
_BRANCHING = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "try", "catch", "finally",
    "function", "return", "exit", "yield_control", "setTimeout", "then", "Promise",
    "break", "continue", "throw", "class", "eval", "Function", "import", "require",
})
_BRANCHING_OPS = frozenset({"?", "?.", "??", "||", "&&", "=>", "??=", "||=", "&&="})
_REGEX_AFTER_WORDS = frozenset({
    "return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void",
    "throw", "await", "yield", "instanceof",
})
_MULTI_OPS = ("??=", "||=", "&&=", "...", "=>", "?.", "??", "||", "&&")
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"(?:0[xXoObB][0-9A-Fa-f_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?)n?")
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


class ScanError(ValueError):
    """The source could not be tokenized (an unterminated string, say)."""


@dataclass
class Scan:
    """What one exec input holds."""

    calls: list[tuple[str, object]] = field(default_factory=list)  # (tool name, argument)
    unparsed: int = 0  # uses of `tools` read as no call
    straight: bool = True  # every call awaited at top level, no branching, nothing unparsed


# -- the result header ----------------------------------------------------------

def header(output) -> tuple[str, str] | None:
    """(state, cell id) from an exec or wait result, or None when the output is
    not Codex's own list form with the header in its first part."""
    if not isinstance(output, list) or not output:
        return None
    first = output[0]
    if not isinstance(first, dict) or first.get("type") not in (None, "input_text"):
        return None
    text = first.get("text")
    if not isinstance(text, str):
        return None
    match = _HEADER.match(text)
    if not match:
        return None
    if match.group(2) is not None:
        return RUNNING, match.group(2)
    return _STATES[match.group(1)], ""


def body(output) -> str:
    """The text the script printed: every part after the header."""
    if not isinstance(output, list):
        return ""
    return "\n".join(str(i["text"]) for i in output[1:]
                     if isinstance(i, dict) and isinstance(i.get("text"), str))


# -- tokens ---------------------------------------------------------------------

def _string(src: str, i: int, quote: str) -> tuple[str, int]:
    """A quoted string literal starting after its quote; (value, index after it)."""
    out = []
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == quote:
            return "".join(out), i + 1
        if ch == "\\":
            value, i = _escape(src, i + 1)
            out.append(value)
            continue
        if ch in "\r\n":
            raise ScanError("newline in a string")
        out.append(ch)
        i += 1
    raise ScanError("unterminated string")


def _escape(src: str, i: int) -> tuple[str, int]:
    if i >= len(src):
        raise ScanError("unterminated escape")
    ch = src[i]
    if ch in _SIMPLE_ESCAPES and not (ch == "0" and src[i + 1:i + 2].isdigit()):
        return _SIMPLE_ESCAPES[ch], i + 1
    if ch == "x":
        digits = src[i + 1:i + 3]
        if not re.fullmatch(r"[0-9A-Fa-f]{2}", digits):
            raise ScanError("bad \\x escape")
        return chr(int(digits, 16)), i + 3
    if ch == "u":
        if src[i + 1:i + 2] == "{":
            end = src.find("}", i + 2)
            digits = src[i + 2:end] if end >= 0 else ""
            if not re.fullmatch(r"[0-9A-Fa-f]{1,6}", digits) or int(digits, 16) > 0x10FFFF:
                raise ScanError("bad \\u{} escape")
            return chr(int(digits, 16)), end + 1
        digits = src[i + 1:i + 5]
        if not re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
            raise ScanError("bad \\u escape")
        return chr(int(digits, 16)), i + 5
    if ch == "\r" and src[i + 1:i + 2] == "\n":
        return "", i + 2
    if ch in "\r\n  ":
        return "", i + 1  # a line continuation
    if ch.isdigit():
        raise ScanError("octal escape")
    return ch, i + 1


def _template(src: str, i: int, tokens: list, depth: int) -> int:
    """A template literal after its backtick. A plain one is a string token;
    one with ${...} is an opaque token, and the code inside is tokenized too."""
    out = []
    n = len(src)
    opaque = False
    while i < n:
        ch = src[i]
        if ch == "`":
            if not opaque:  # an opaque one was added at its first ${
                tokens.append(("str", "".join(out)))
            return i + 1
        if ch == "\\":
            value, i = _escape(src, i + 1)
            out.append(value)
            continue
        if ch == "$" and src[i + 1:i + 2] == "{":
            if not opaque:
                tokens.append(("tmpl", None))
            opaque = True
            i = _tokens(src, i + 2, tokens, depth + 1, stop_at_brace=True)
            continue
        out.append(ch)
        i += 1
    raise ScanError("unterminated template")


def _regex_allowed(tokens: list) -> bool:
    if not tokens:
        return True
    kind, value = tokens[-1]
    if kind in ("ident",):
        return value in _REGEX_AFTER_WORDS
    if kind in ("num", "str", "tmpl", "regex"):
        return False
    return value not in (")", "]", "}")


def _regex(src: str, i: int) -> int:
    """A regex literal after its opening slash; the index after its flags."""
    n = len(src)
    in_class = False
    while i < n:
        ch = src[i]
        if ch in "\r\n":
            raise ScanError("newline in a regex")
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            i += 1
            while i < n and (src[i].isalnum() or src[i] in "_$"):
                i += 1
            return i
        i += 1
    raise ScanError("unterminated regex")


def _tokens(src: str, i: int, tokens: list, depth: int = 0,
            stop_at_brace: bool = False) -> int:
    if depth > MAX_DEPTH:
        raise ScanError("template nesting too deep")
    n = len(src)
    braces = 0
    while i < n:
        ch = src[i]
        if ch.isspace():
            i += 1
            continue
        if src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end < 0 else end + 1
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end < 0:
                raise ScanError("unterminated comment")
            i = end + 2
            continue
        if ch in "\"'":
            value, i = _string(src, i + 1, ch)
            tokens.append(("str", value))
            continue
        if ch == "`":
            i = _template(src, i + 1, tokens, depth)
            continue
        if ch == "/" and _regex_allowed(tokens):
            i = _regex(src, i + 1)
            tokens.append(("regex", None))
            continue
        match = _IDENT.match(src, i)
        if match:
            tokens.append(("ident", match.group()))
            i = match.end()
            continue
        match = _NUMBER.match(src, i) if (ch.isdigit() or ch == ".") else None
        if match and match.end() > i and match.group() != ".":
            tokens.append(("num", match.group()))
            i = match.end()
            continue
        if stop_at_brace:
            if ch == "{":
                braces += 1
            elif ch == "}":
                if braces == 0:
                    return i + 1
                braces -= 1
        op = next((o for o in _MULTI_OPS if src.startswith(o, i)), ch)
        tokens.append(("op", op))
        i += len(op)
    if stop_at_brace:
        raise ScanError("unterminated template substitution")
    return i


def tokenize(src: str) -> list[tuple[str, object]]:
    tokens: list[tuple[str, object]] = []
    _tokens(src, 0, tokens)
    return tokens


# -- literals -------------------------------------------------------------------

class _NotLiteral(ValueError):
    pass


def _number(text: str) -> object:
    text = text.replace("_", "")
    if text.endswith("n"):
        raise _NotLiteral("bigint")
    try:
        if text[:2].lower() in ("0x", "0o", "0b"):
            return int(text, 0)
        value = float(text)
        return int(value) if value.is_integer() and "." not in text and "e" not in text.lower() \
            else value
    except ValueError as exc:
        raise _NotLiteral(text) from exc


def _literal(tokens: list, i: int, depth: int = 0) -> tuple[object, int]:
    """A plain literal value at tokens[i]; (value, index after it)."""
    if depth > MAX_DEPTH or i >= len(tokens):
        raise _NotLiteral("too deep or cut off")
    kind, value = tokens[i]
    if kind == "str":
        return value, i + 1
    if kind == "num":
        return _number(value), i + 1
    if kind == "ident" and value in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[value], i + 1
    if kind == "op" and value == "-" and i + 1 < len(tokens) and tokens[i + 1][0] == "num":
        number = _number(tokens[i + 1][1])
        return -number, i + 2
    if kind == "op" and value == "[":
        items, i = [], i + 1
        while True:
            if i < len(tokens) and tokens[i] == ("op", "]"):
                return items, i + 1
            item, i = _literal(tokens, i, depth + 1)
            items.append(item)
            if i < len(tokens) and tokens[i] == ("op", ","):
                i += 1
            elif not (i < len(tokens) and tokens[i] == ("op", "]")):
                raise _NotLiteral("array")
    if kind == "op" and value == "{":
        obj, i = {}, i + 1
        while True:
            if i < len(tokens) and tokens[i] == ("op", "}"):
                return obj, i + 1
            if i + 1 >= len(tokens) or tokens[i][0] not in ("ident", "str") \
                    or tokens[i + 1] != ("op", ":"):
                raise _NotLiteral("object key")
            key = str(tokens[i][1])
            item, i = _literal(tokens, i + 2, depth + 1)
            obj[key] = item
            if i < len(tokens) and tokens[i] == ("op", ","):
                i += 1
            elif not (i < len(tokens) and tokens[i] == ("op", "}")):
                raise _NotLiteral("object")
    raise _NotLiteral(f"{kind} {value!r}")


def _argument(tokens: list, i: int) -> tuple[object, int]:
    """The one argument of a call whose "(" is at tokens[i-1]; (value, index after ")")."""
    if i < len(tokens) and tokens[i] == ("op", ")"):
        return NO_ARG, i + 1
    value, i = _literal(tokens, i)
    if i < len(tokens) and tokens[i] == ("op", ","):
        i += 1
    if i < len(tokens) and tokens[i] == ("op", ")"):
        return value, i + 1
    raise _NotLiteral("more than one argument")


# -- the scan -------------------------------------------------------------------

def scan(source: str) -> Scan:
    """The nested tool calls an exec input makes, read conservatively."""
    result = Scan()
    if not isinstance(source, str):
        result.unparsed, result.straight = 1, False
        return result
    if len(source) > MAX_SOURCE:
        result.unparsed, result.straight = 1, False
        return result
    try:
        tokens = tokenize(source)
    except (ScanError, RecursionError):
        # Nothing in it can be read with certainty: one unparsed count, no events.
        result.unparsed = max(1, len(re.findall(r"\btools\b", source)))
        result.straight = False
        return result
    i = 0
    while i < len(tokens):
        kind, value = tokens[i]
        if kind == "ident" and value in _BRANCHING:
            result.straight = False
        elif kind == "op" and value in _BRANCHING_OPS:
            result.straight = False
        if kind != "ident" or value != "tools":
            i += 1
            continue
        before = tokens[i - 1] if i else None
        after = tokens[i + 1] if i + 1 < len(tokens) else None
        if before == ("op", ".") or (after == ("op", ":")
                                     and before in (("op", "{"), ("op", ","))):
            i += 1  # a property named tools, or an object key: not the global
            continue
        name = tokens[i + 2] if i + 2 < len(tokens) else None
        opened = tokens[i + 3] if i + 3 < len(tokens) else None
        if (after != ("op", ".") or name is None or name[0] != "ident"
                or opened != ("op", "(")):
            result.unparsed += 1  # tools[...], an alias, a reference without a call
            result.straight = False
            i += 1
            continue
        try:
            arg, end = _argument(tokens, i + 4)
        except _NotLiteral:
            result.unparsed += 1
            result.straight = False
            i += 4  # calls inside the argument are still read
            continue
        if before != ("ident", "await"):
            result.straight = False  # an unawaited call may never finish
        if len(result.calls) >= MAX_CALLS:
            result.unparsed += 1
            result.straight = False
        else:
            result.calls.append((str(name[1]), arg))
        i = end
    return result
