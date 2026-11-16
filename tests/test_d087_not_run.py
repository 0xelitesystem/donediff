"""A command that did not run reads UNVERIFIED, never CONTRADICTED (D087,
Agent Security ruling (b), 2026-11-16).

Each reader's own record for a declined, skipped or cancelled call, in the
shapes the agents' sources write (cited in each reader), plus the guards that
keep a real failure CONTRADICTED: a command's own output never turns a failure
into "did not run". A write that did not run stays no evidence. Synthetic
sessions only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from donediff import cli
from donediff.models import ClaimType, EventKind, Verdict
from donediff.verify import NO_EVIDENCE_STATUS

CONFORMANCE = Path(__file__).resolve().parent / "conformance"
sys.path.insert(0, str(CONFORMANCE))

from _common import CONTENT, PROMPT, TEST_CMD, load_fixture, target  # noqa: E402

CLAIM = "All tests pass."


def _verdict(path, agent: str, claim_type=ClaimType.TESTS_PASS):
    result = cli.run_audit(str(path), check_disk=False, agent=agent)
    found = [f for f in result.findings if f.claim.type is claim_type]
    assert len(found) == 1, [(f.claim.type, f.verdict) for f in result.findings]
    return found[0], [e for e in result.session.events if e.kind is EventKind.TOOL_CALL]


def _not_run(calls, status: str) -> None:
    runs = [e for e in calls if e.check_types]
    assert runs, "no test run read"
    for e in runs:
        assert (e.is_error, e.exit_code, e.status, e.failure_hint) == (False, None, status, False)
        assert e.status in NO_EVIDENCE_STATUS


# -- Claude Code --------------------------------------------------------------

_CC_REJECT = ("The user doesn't want to proceed with this tool use. The tool use was rejected "
              "(eg. if it was a file edit, the new_string was NOT written to the file).")


def _cc(tmp_path, text: str, is_error: bool = True, use=None, name="Bash",
        tool_input=None, claim=CLAIM) -> Path:
    def rec(kind, second, content, **extra):
        return {"type": kind, "timestamp": f"2026-06-10T12:00:{second:02d}.000Z",
                "sessionId": "d087", "cwd": str(tmp_path),
                "message": {"role": "assistant" if kind == "assistant" else "user",
                            "content": content}, **extra}
    records = [
        rec("user", 0, [{"type": "text", "text": PROMPT}]),
        rec("assistant", 1, [{"type": "tool_use", "id": "t1", "name": name,
                              "input": tool_input or {"command": TEST_CMD}}]),
        rec("user", 2, [{"type": "tool_result", "tool_use_id": "t1", "content": text,
                         "is_error": is_error}], toolUseResult=use if use is not None else text),
        rec("assistant", 3, [{"type": "text", "text": claim}]),
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


@pytest.mark.parametrize("text,status", [
    (_CC_REJECT, "declined"),
    (_CC_REJECT + " To tell you how to proceed, the user said:\nrun it later", "declined"),
    ("[Request interrupted by user for tool use]", "cancelled"),
])
def test_claude_code_did_not_run(tmp_path, text, status):
    finding, calls = _verdict(_cc(tmp_path, text), "claude-code")
    assert finding.verdict is Verdict.UNVERIFIED, finding.evidence
    _not_run(calls, status)


@pytest.mark.parametrize("text,use", [
    ("Exit code 1\n" + _CC_REJECT, None),          # a real exit code: it ran
    (_CC_REJECT, {"stdout": _CC_REJECT, "stderr": "", "interrupted": False}),  # it printed this
    ("1 failed, 15 passed\n" + _CC_REJECT, None),  # not Claude Code's record
])
def test_claude_code_real_failure_stays_contradicted(tmp_path, text, use):
    finding, _ = _verdict(_cc(tmp_path, text, use=use), "claude-code")
    assert finding.verdict is Verdict.CONTRADICTED, finding.evidence


def test_claude_code_rejected_write_is_still_no_evidence(tmp_path):
    (tmp_path / "x.py").write_text(CONTENT, encoding="utf-8")
    path = _cc(tmp_path, _CC_REJECT, name="Write",
               tool_input={"file_path": target(tmp_path), "content": CONTENT},
               claim="I created `x.py` with the change.")
    finding, calls = _verdict(path, "claude-code", ClaimType.FILE_CREATED)
    assert finding.verdict is not Verdict.VERIFIED, finding.evidence
    assert calls[0].is_error and calls[0].status == "declined"


# -- Goose ---------------------------------------------------------------------

mk = load_fixture("goose", "make_goose.py")

_GOOSE_DECLINED = ("The user has declined to run this tool. DO NOT attempt to call this tool "
                   "again. If there are no alternative methods to proceed, clearly explain "
                   "the situation and STOP.")


def _goose(tmp_path, value=None, name="developer__shell", args=None, claim=CLAIM, **kw):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_9", cwd=str(tmp_path))
    b.user(PROMPT)
    b.tool(name, args or {"command": TEST_CMD}, value, **kw)
    b.say(claim)
    return b.close()


def _goose_text(text, is_error=True, **extra):
    return {"content": [{"type": "text", "text": text}], "isError": is_error, **extra}


@pytest.mark.parametrize("text,status", [
    (_GOOSE_DECLINED, "declined"),
    ("The user declined to run this tool.", "declined"),
    ("This tool call was not handled.", "skipped"),
])
def test_goose_did_not_run(tmp_path, text, status):
    finding, calls = _verdict(_goose(tmp_path, _goose_text(text)), "goose")
    assert finding.verdict is Verdict.UNVERIFIED, finding.evidence
    _not_run(calls, status)


def test_goose_real_exit_code_stays_contradicted(tmp_path):
    value = _goose_text(_GOOSE_DECLINED, structuredContent={
        "stdout": "", "stderr": _GOOSE_DECLINED, "exit_code": 1})
    finding, _ = _verdict(_goose(tmp_path, value), "goose")
    assert finding.verdict is Verdict.CONTRADICTED, finding.evidence


def test_goose_declined_write_is_still_no_evidence(tmp_path):
    (tmp_path / "x.py").write_text(CONTENT, encoding="utf-8")
    path = _goose(tmp_path, _goose_text(_GOOSE_DECLINED), name="developer__write",
                  args={"path": target(tmp_path), "content": CONTENT},
                  claim="I created `x.py` with the change.")
    finding, calls = _verdict(path, "goose", ClaimType.FILE_CREATED)
    assert finding.verdict is not Verdict.VERIFIED, finding.evidence
    assert calls[0].is_error and calls[0].status == "declined"


# -- Codex ---------------------------------------------------------------------

mc = load_fixture("codex", "make_codex.py")


def _codex(tmp_path, output=None, item_status=None) -> Path:
    clock = mc.Clock()
    records = [clock.line("session_meta", {**mc.meta("d087", "paginated"), "cwd": str(tmp_path)}),
               clock.line("response_item", mc.message("user", PROMPT)),
               clock.line("response_item", {
                   "type": "function_call", "name": "exec_command", "call_id": "c1",
                   "arguments": json.dumps({"cmd": TEST_CMD, "workdir": str(tmp_path)})})]
    if output is not None:
        records.append(clock.line("response_item", {"type": "function_call_output",
                                                    "call_id": "c1", "output": output}))
    if item_status is not None:
        records.append(clock.line("event_msg", {
            "type": "item_completed", "thread_id": "d087", "turn_id": "turn-1",
            "completed_at_ms": 0,
            "item": {"type": "CommandExecution", "id": "c1", "command": ["bash", "-lc", TEST_CMD],
                     "cwd": "file://" + str(tmp_path), "parsed_cmd": [], "source": "agent",
                     "status": item_status, "aggregated_output": "", "exit_code": None}}))
    records.append(clock.line("response_item", mc.message("assistant", CLAIM)))
    return mc.write(tmp_path / "rollout-2026-06-10T12-00-00-d087.jsonl", records)


@pytest.mark.parametrize("output,item,status", [
    ({"content": "exec command rejected by user", "success": False}, None, "declined"),
    ("exec command rejected by user", "declined", "declined"),
    (None, "declined", "declined"),
    ("aborted", None, "cancelled"),
    ("aborted before dispatch", None, "cancelled"),
    ({"content": "aborted by user after 2.5s", "success": False}, None, "cancelled"),
    ("Wall time: 1.0 seconds\naborted by user", None, "cancelled"),
])
def test_codex_did_not_run(tmp_path, output, item, status):
    finding, calls = _verdict(_codex(tmp_path, output, item), "codex")
    assert finding.verdict is Verdict.UNVERIFIED, finding.evidence
    _not_run(calls, status)


@pytest.mark.parametrize("output,item", [
    ({"content": "sandbox setup failed", "success": False}, None),
    (mc.exec_output(1, "exec command rejected by user"), "failed"),
    ({"content": "exec command rejected by user\n1 failed", "success": False}, None),
])
def test_codex_real_failure_stays_contradicted(tmp_path, output, item):
    finding, _ = _verdict(_codex(tmp_path, output, item), "codex")
    assert finding.verdict is Verdict.CONTRADICTED, finding.evidence


# -- opencode ------------------------------------------------------------------

mo = load_fixture("opencode", "make_opencode.py")


@pytest.mark.parametrize("schema", ["v1", "v2"])
@pytest.mark.parametrize("error,status", [
    ("The user rejected permission to use this specific tool call.", "declined"),
    ("The user rejected permission to use this specific tool call with the following "
     "feedback: not now", "declined"),
    ("Tool execution aborted", "cancelled"),
])
def test_opencode_did_not_run(tmp_path, schema, error, status):
    b = mo.Builder(tmp_path / "opencode.db", schema).session("ses_d0870001",
                                                              directory=str(tmp_path))
    b.user(PROMPT)
    b.tool(mo.SHELL_TOOL[schema], {"command": TEST_CMD}, status="error", error=error)
    b.say(CLAIM)
    finding, calls = _verdict(b.close(), "opencode")
    assert finding.verdict is Verdict.UNVERIFIED, finding.evidence
    _not_run(calls, status)


# -- Cline ---------------------------------------------------------------------

mcl = load_fixture("cline", "make_cline.py")


@pytest.mark.parametrize("text,status", [
    ("The user denied this operation.", "declined"),
    ("The user denied this operation. The user provided the following feedback:\n"
     "<feedback>\nnot now\n</feedback>", "declined"),
    ("Tool execution was cancelled because the user sent a follow-up message.", "cancelled"),
])
def test_cline_did_not_run(tmp_path, text, status):
    doc = mcl.Doc(session_id="1781000000000_d087", cwd=str(tmp_path))
    doc.user(PROMPT)
    doc.assistant(calls=[mcl.call("toolu_1", "run_commands", {"commands": [TEST_CMD]})])
    doc.results(mcl.result("toolu_1", "run_commands", text, is_error=True))
    doc.assistant(CLAIM)
    path = mcl.place(tmp_path / "cline-sessions", doc.document(), cwd=str(tmp_path))
    finding, calls = _verdict(path, "cline")
    assert finding.verdict is Verdict.UNVERIFIED, finding.evidence
    _not_run(calls, status)


def test_cline_real_failure_stays_contradicted(tmp_path):
    doc = mcl.Doc(session_id="1781000000000_d088", cwd=str(tmp_path))
    doc.user(PROMPT)
    doc.assistant(calls=[mcl.call("toolu_1", "run_commands", {"commands": [TEST_CMD]})])
    doc.results(mcl.result("toolu_1", "run_commands",
                           [mcl.command_op(TEST_CMD, 1, "1 failed, 15 passed in 1.1s")]))
    doc.assistant(CLAIM)
    path = mcl.place(tmp_path / "cline-sessions", doc.document(), cwd=str(tmp_path))
    finding, _ = _verdict(path, "cline")
    assert finding.verdict is Verdict.CONTRADICTED, finding.evidence
