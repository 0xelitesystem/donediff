"""SEC-207 (a deeply nested JSON line crashed the audit) and SEC-208 (a failed
or cancelled write still verified "created X"), for every reader."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from donediff import cli, parser
from donediff.models import ClaimType, Verdict
from donediff.readers import CODEX, GEMINI, OPENCODE, QWEN
from donediff.readers.claude_code import CLAUDE_CODE

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
DEPTH = 200_000
BOMB = "[" * DEPTH + "]" * DEPTH
DOCUMENTED_EXITS = (0, 1, 2, 3)
FAKE_SRC = r"C:\fake\project\src\a.py"  # inside the conftest cwd
CLAIM = "I created `limiter.py` with the new limiter."


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mg = _load("make_gemini_sec", FIXTURES / "gemini" / "make_gemini.py")
mq = _load("make_qwen_sec", FIXTURES / "qwen" / "make_qwen.py")
mc = _load("make_codex_sec", FIXTURES / "codex" / "make_codex.py")
mo = _load("make_opencode_sec", FIXTURES / "opencode" / "make_opencode.py")


def run_cli(path, agent, capsys, disk=False):
    argv = ["audit", str(path), "--agent", agent, "--no-color"]
    if not disk:
        argv.append("--no-disk-check")
    code = cli.main(argv)  # a crash raises here and fails the test
    out, err = capsys.readouterr()
    assert code in DOCUMENTED_EXITS
    assert "Traceback" not in out + err
    return code, out


def append_line(path, text: str) -> None:
    with open(path, "ab") as fh:
        fh.write(b"\n" + text.encode() + b"\n")


# -- SEC-207: the helper and the guard -----------------------------------------

def test_loads_turns_a_nesting_bomb_into_value_error():
    with pytest.raises(ValueError, match="too deep"):
        parser.loads(BOMB)
    assert parser.loads('{"a": [1, 2]}') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parser.loads("{not json")


def test_no_bare_json_load_outside_the_helper():
    """Every JSON parse under donediff/ goes through parser.loads."""
    bare = re.compile(r"\bjson\.loads?\s*\(")
    offenders = []
    for path in sorted((ROOT / "donediff").rglob("*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if bare.search(line):
                offenders.append(path.relative_to(ROOT).as_posix())
    # The one allowed call is inside parser.loads itself.
    assert offenders == ["donediff/parser.py"]


# -- SEC-207: one bomb line per reader -----------------------------------------

def test_claude_code_bomb_line_is_skipped(tmp_path, capsys):
    path = write_jsonl(tmp_path / "s.jsonl", [assistant_text("Before."), assistant_text("After.")])
    append_line(path, BOMB)
    append_line(path, json.dumps(assistant_text("Last.")))
    session = CLAUDE_CODE.parse(Path(path))
    assert [e.text for e in session.text_events()] == ["Before.", "After.", "Last."]
    run_cli(path, "claude-code", capsys)
    run_cli(path, "auto", capsys)


def test_claude_code_bomb_first_line(tmp_path, capsys):
    path = tmp_path / "s.jsonl"
    path.write_text(BOMB + "\n" + json.dumps(assistant_text("After.")) + "\n", encoding="utf-8")
    run_cli(path, "claude-code", capsys)
    run_cli(path, "auto", capsys)


def test_codex_bomb_line_is_counted(tmp_path, capsys):
    clock = mc.Clock()
    records = [clock.line("session_meta", mc.meta("s1", "legacy")),
               clock.line("response_item", mc.message("assistant", "Still read."))]
    path = mc.write(tmp_path / "rollout-2026-06-10T12-00-00-s1.jsonl", records)
    append_line(path, BOMB)
    session = CODEX.parse(path)
    assert session.unrecognised == 1
    assert [e.text for e in session.text_events()] == ["Still read."]
    _, out = run_cli(path, "codex", capsys)
    assert "1 unrecognised record(s) skipped" in out
    run_cli(path, "auto", capsys)


def test_codex_bomb_first_line(tmp_path, capsys):
    path = tmp_path / "rollout-2026-06-10T12-00-00-s1.jsonl"
    path.write_text(BOMB + "\n", encoding="utf-8")
    run_cli(path, "codex", capsys)
    run_cli(path, "auto", capsys)


def test_gemini_bomb_line_is_counted(tmp_path, capsys):
    clock = mg.Clock()
    records = [mg.meta(), mg.user(clock, "u1", "Fix it."), mg.gemini(clock, "g1", "Still read.")]
    path = mg.write(tmp_path / "session-2026-06-10T12-00-6f1c2d3e.jsonl", records)
    append_line(path, BOMB)
    session = GEMINI.parse(path)
    assert session.unrecognised == 1
    assert [e.text for e in session.text_events()] == ["Still read."]
    _, out = run_cli(path, "gemini", capsys)
    assert "1 unrecognised record(s) skipped" in out
    run_cli(path, "auto", capsys)


def test_gemini_bomb_first_line_and_old_json(tmp_path, capsys):
    first = tmp_path / "session-2026-06-10T12-00-6f1c2d3e.jsonl"
    first.write_text(BOMB + "\n", encoding="utf-8")
    run_cli(first, "gemini", capsys)
    run_cli(first, "auto", capsys)
    old = tmp_path / "session-2026-06-10T12-00-6f1c2d3e.json"
    old.write_text(BOMB, encoding="utf-8")
    run_cli(old, "gemini", capsys)
    run_cli(old, "auto", capsys)


def test_qwen_bomb_line_is_counted(tmp_path, capsys):
    chat = mq.Chat()
    chat.user("Fix it.")
    chat.assistant("Still read.")
    path = mq.write(tmp_path / f"{mq.SID}.jsonl", chat.records)
    append_line(path, BOMB)
    session = QWEN.parse(path)
    assert session.unrecognised == 1
    assert [e.text for e in session.text_events()] == ["Still read."]
    _, out = run_cli(path, "qwen", capsys)
    assert "1 unrecognised record(s) skipped" in out
    run_cli(path, "auto", capsys)


def test_qwen_bomb_first_line(tmp_path, capsys):
    path = tmp_path / f"{mq.SID}.jsonl"
    path.write_text(BOMB + "\n", encoding="utf-8")
    run_cli(path, "qwen", capsys)
    run_cli(path, "auto", capsys)


@pytest.mark.parametrize("schema", ("v1", "v2"))
def test_opencode_bomb_part_is_counted(tmp_path, capsys, schema):
    b = mo.Builder(tmp_path / f"{schema}.db", schema).session("ses_t0001")
    b.user("Fix it.")
    b.say("Still read.")
    if schema == "v2":
        b.raw_message("assistant", BOMB)
    else:
        b._part(b._assistant(), {"type": "text", "text": "bombpart"})
        b.conn.execute("UPDATE part SET data = ? WHERE data LIKE '%bombpart%'", (BOMB,))
    target = b.close()
    session = OPENCODE.parse(OPENCODE.resolve(target))
    assert session.unrecognised == 1
    assert [e.text for e in session.text_events()] == ["Still read."]
    run_cli(target, "opencode", capsys)


# -- SEC-208: a write that did not land is no evidence -------------------------

def _claude_code(tmp_path, ok: bool):
    records = [
        assistant_tool("w1", "Write", {"file_path": f"{tmp_path}/limiter.py", "content": "x"}),
        tool_result("w1", "ok" if ok else "The user doesn't want to proceed with this tool use.",
                    is_error=not ok),
        assistant_text(CLAIM),
    ]
    return write_jsonl(tmp_path / "s.jsonl", records), "claude-code"


def _codex(tmp_path, ok: bool):
    clock = mc.Clock()
    records = [clock.line("session_meta", {**mc.meta("s1", "legacy"), "cwd": str(tmp_path)})]
    records += mc.patch_call(clock, "p1", f"*** Begin Patch\n*** Add File: {tmp_path}/limiter.py\n"
                             "+x = 1\n*** End Patch\n", f"{tmp_path}/limiter.py")
    if not ok:
        records.append(clock.line("event_msg", {"type": "patch_apply_end", "call_id": "p1",
                                                "success": False}))
    records.append(clock.line("response_item", mc.message("assistant", CLAIM)))
    return mc.write(tmp_path / "rollout-2026-06-10T12-00-00-s1.jsonl", records), "codex"


def _gemini(tmp_path, status: str):
    clock = mg.Clock()
    result = (mg.output("c1", "write_file", "ok") if status == "success"
              else mg.error("c1", "write_file", f"Tool call {status}."))
    records = [mg.meta(), mg.user(clock, "u1", "Add a limiter."),
               mg.gemini(clock, "g1", "", [mg.call(clock, "c1", "write_file",
                                                   {"file_path": f"{tmp_path}/limiter.py",
                                                    "content": "x = 1\n"}, status, result)]),
               mg.gemini(clock, "g2", CLAIM)]
    return mg.write(tmp_path / "session-2026-06-10T12-00-6f1c2d3e.jsonl", records), "gemini"


def _qwen(tmp_path, status: str):
    chat = mq.Chat()
    chat.user("Add a limiter.")
    chat.assistant(calls=[mq.call("c1", "write_file", {"file_path": f"{tmp_path}/limiter.py",
                                                        "content": "x = 1\n"})])
    if status == "success":
        chat.result("c1", "write_file", output="Successfully created limiter.py")
    else:
        chat.result("c1", "write_file", error=f"Tool call {status}.", status=status)
    chat.assistant(CLAIM)
    return mq.write(tmp_path / f"{mq.SID}.jsonl", chat.records), "qwen"


def _opencode(tmp_path, ok: bool, schema="v2"):
    b = mo.Builder(tmp_path / f"{schema}.db", schema).session("ses_t0001", directory=str(tmp_path))
    b.user("Add a limiter.")
    args = {mo.PATH_KEY[schema]: f"{tmp_path}/limiter.py", "content": "x = 1\n"}
    if ok:
        b.tool("write", args, output="Wrote file successfully.")
    else:
        b.tool("write", args, status="error", error="The user rejected permission.")
    b.say(CLAIM)
    return b.close(), "opencode"


FAILED = [
    ("claude-code", lambda p: _claude_code(p, False), "error"),
    ("codex", lambda p: _codex(p, False), "error"),
    ("gemini-cancelled", lambda p: _gemini(p, "cancelled"), "cancelled"),
    ("gemini-error", lambda p: _gemini(p, "error"), "error"),
    ("qwen-cancelled", lambda p: _qwen(p, "cancelled"), "cancelled"),
    ("qwen-error", lambda p: _qwen(p, "error"), "error"),
    ("opencode-v1", lambda p: _opencode(p, False, "v1"), "error"),
    ("opencode-v2", lambda p: _opencode(p, False, "v2"), "error"),
]
SUCCEEDED = [
    ("claude-code", lambda p: _claude_code(p, True)),
    ("codex", lambda p: _codex(p, True)),
    ("gemini", lambda p: _gemini(p, "success")),
    ("qwen", lambda p: _qwen(p, "success")),
    ("opencode", lambda p: _opencode(p, True)),
]


def _file_finding(result):
    found = [f for f in result.findings
             if f.claim.type in (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED)]
    assert len(found) == 1, [f.claim for f in result.findings]
    return found[0]


@pytest.mark.parametrize("disk", (False, True), ids=("no-disk-check", "disk-check"))
@pytest.mark.parametrize("name,build,status", FAILED, ids=[f[0] for f in FAILED])
def test_failed_write_does_not_verify_created(tmp_path, name, build, status, disk):
    target, agent = build(tmp_path)
    finding = _file_finding(cli.run_audit(str(target), check_disk=disk, agent=agent))
    assert finding.verdict is Verdict.UNVERIFIED
    assert "did not succeed" in finding.evidence
    assert f"({status})" in finding.evidence


@pytest.mark.parametrize("name,build", SUCCEEDED, ids=[s[0] for s in SUCCEEDED])
def test_successful_write_still_verifies(tmp_path, name, build):
    target, agent = build(tmp_path)
    finding = _file_finding(cli.run_audit(str(target), check_disk=False, agent=agent))
    assert finding.verdict is Verdict.VERIFIED


def test_failed_write_still_makes_a_check_stale(tmp_path):
    """A failed edit after a passing run still counts as 'code may have changed'."""
    records = [
        assistant_tool("e0", "Edit", {"file_path": FAKE_SRC,"old_string": "1",
                                      "new_string": "2"}),
        tool_result("e0", "ok"),
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        tool_result("t1", "3 passed in 0.10s"),
        assistant_tool("e1", "Edit", {"file_path": FAKE_SRC,"old_string": "2",
                                      "new_string": "3"}),
        tool_result("e1", "String not found in file.", is_error=True),
        assistant_text("All tests pass."),
    ]
    path = write_jsonl(tmp_path / "s.jsonl", records)
    result = cli.run_audit(path, check_disk=False, agent="claude-code")
    tests = [f for f in result.findings if f.claim.type is ClaimType.TESTS_PASS]
    assert tests and tests[0].verdict is not Verdict.VERIFIED


def test_failed_then_successful_write_verifies(tmp_path):
    records = [
        assistant_tool("w1", "Write", {"file_path": f"{tmp_path}/limiter.py", "content": "x"}),
        tool_result("w1", "denied", is_error=True),
        assistant_tool("w2", "Write", {"file_path": f"{tmp_path}/limiter.py", "content": "x"}),
        tool_result("w2", "ok"),
        assistant_text(CLAIM),
    ]
    path = write_jsonl(tmp_path / "s.jsonl", records)
    finding = _file_finding(cli.run_audit(path, check_disk=False, agent="claude-code"))
    assert finding.verdict is Verdict.VERIFIED


@pytest.mark.parametrize("disk", (False, True), ids=("no-disk-check", "disk-check"))
def test_failed_write_does_not_verify_report_path(tmp_path, disk):
    """The same hole in 'the report is at <path>' (verify._verify_path_written)."""
    records = [
        assistant_tool("w1", "Write", {"file_path": str(tmp_path / "out" / "audit.md"),
                                       "content": "ok"}),
        tool_result("w1", "The user doesn't want to proceed with this tool use.", is_error=True),
        assistant_text("The report is at `out/audit.md`."),
    ]
    for record in records:
        record["cwd"] = str(tmp_path)
    path = write_jsonl(tmp_path / "s.jsonl", records)
    result = cli.run_audit(path, check_disk=disk, agent="claude-code")
    found = [f for f in result.findings if f.claim.type is ClaimType.PATH_WRITTEN]
    assert len(found) == 1
    assert found[0].verdict is Verdict.UNVERIFIED and "did not succeed" in found[0].evidence
