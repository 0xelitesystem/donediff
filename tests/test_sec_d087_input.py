"""D087 pen test, unit C: input robustness of the readers, the parser and the report.

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
from donediff import cli
from donediff.evidence import log_int, passed_counts
from donediff.models import ClaimType, Verdict

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / "fixtures" / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mc = _load("make_codex_d087c", "codex/make_codex.py")
mg = _load("make_gemini_d087c", "gemini/make_gemini.py")
mq = _load("make_qwen_d087c", "qwen/make_qwen.py")
ml = _load("make_cline_d087c", "cline/make_cline.py")
mgo = _load("make_goose_d087c", "goose/make_goose.py")
mo = _load("make_opencode_d087c", "opencode/make_opencode.py")


def audit(target, agent: str):
    return cli.run_audit(str(target), check_disk=False, agent=agent)


def pass_verdict(result) -> Verdict:
    found = [f for f in result.findings if f.claim.type is ClaimType.TESTS_PASS]
    assert len(found) == 1
    return found[0].verdict


@contextlib.contextmanager
def any_int_digits():
    """Let the fixture builders format a huge number (the reader must cope with it)."""
    old = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else None
    if old is not None:
        sys.set_int_max_str_digits(0)
    try:
        yield
    finally:
        if old is not None:
            sys.set_int_max_str_digits(old)


# -- SEC-228: a number over 4300 digits in tool output -------------------------

HUGE = "9" * 5000


def test_log_int_caps_digits():
    assert log_int("42") == 42
    assert log_int("-3") == -3
    assert log_int("0" * 5000 + "7") == 7
    assert log_int(HUGE) is None
    assert log_int(HUGE, 1) == 1
    assert log_int("-" + HUGE, 1) == 1


def test_passed_counts_overlong_is_no_count():
    assert passed_counts(HUGE + " passed") == frozenset()
    assert passed_counts("3 passed\n" + HUGE + " passed") == frozenset()
    assert passed_counts(HUGE + " passed\n3 passed") == frozenset({3})


def test_claim_count_overlong_is_no_claim(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [assistant_text(f"{HUGE} of {HUGE} tests pass.")])
    result = audit(path, "claude-code")
    assert not [f for f in result.findings if f.claim.type is ClaimType.TEST_COUNT]


def _codex(tmp_path, out, code):
    with any_int_digits():
        c = mc.Clock()
        recs = [c.line("session_meta", mc.meta("s1", "legacy")),
                c.line("response_item", {"type": "function_call", "name": "exec_command",
                                         "call_id": "x1", "arguments": json.dumps({"cmd": "pytest -q"})}),
                c.line("response_item", {"type": "function_call_output", "call_id": "x1",
                                         "output": mc.exec_output(code, out)}),
                c.line("response_item", mc.message("assistant", "All tests pass."))]
        return mc.write(tmp_path / "rollout-2026-06-10T12-00-00-s1.jsonl", recs)


def _gemini(tmp_path, out, code):
    with any_int_digits():
        c = mg.Clock()
        recs = [mg.meta(), mg.user(c, "u1", "Run tests."),
                mg.gemini(c, "g1", "", [mg.shell_call(c, "c1", "pytest -q", code, out)]),
                mg.gemini(c, "g2", "All tests pass.")]
        return mg.write(tmp_path / "session-2026-06-10T12-00-6f1c2d3a.jsonl", recs)


def _qwen(tmp_path, out, code):
    with any_int_digits():
        ch = mq.Chat()
        ch.user("Run tests.")
        mq.shell(ch, "c1", "pytest -q", code, out, structured=False)
        ch.assistant("All tests pass.")
        return mq.write(tmp_path / f"{mq.SID}.jsonl", ch.records)


def _cline(tmp_path, out, code):
    with any_int_digits():
        doc = ml.Doc()
        doc.user("Run tests.")
        ml.shell(doc, "c1", "pytest -q", code, out)
        doc.assistant("All tests pass.")
        return ml.write(tmp_path / f"{ml.SID}.messages.json", doc.document())


def _goose(tmp_path, out, code):
    b = mgo.Builder(tmp_path / "goose.db").session("20260610_1")
    b.user("Run tests.")
    b.shell("pytest -q", out, code)
    b.say("All tests pass.")
    return b.close()


def _opencode(tmp_path, out, code):
    o = mo.Builder(tmp_path / "oc.db", "v2").session("ses_t0001")
    o.user("Run tests.")
    o.shell("pytest -q", out, code)
    o.say("All tests pass.")
    return o.close()


def _claude(tmp_path, out, code):
    with any_int_digits():
        text = out if code is None else f"Exit code {code}\n{out}"
    # is_error false: only the printed code can make this run a failure.
    return write_jsonl(tmp_path / "cc.jsonl", [
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        tool_result("t1", text, is_error=False),
        assistant_text("All tests pass.")])


BUILDERS = {"claude-code": _claude, "codex": _codex, "gemini": _gemini, "qwen": _qwen,
            "cline": _cline, "goose": _goose, "opencode": _opencode}


@pytest.mark.parametrize("agent", sorted(BUILDERS))
def test_overlong_passed_count_reads(tmp_path, agent):
    code = None if agent in ("claude-code", "gemini") else 0
    result = audit(BUILDERS[agent](tmp_path, HUGE + " passed in 0.1s", code), agent)
    assert pass_verdict(result) is not Verdict.CONTRADICTED


@pytest.mark.parametrize("agent", ["claude-code", "codex", "gemini", "qwen", "cline"])
def test_overlong_exit_code_is_a_failure(tmp_path, agent):
    with any_int_digits():
        code = int(HUGE)
    result = audit(BUILDERS[agent](tmp_path, "boom", code), agent)
    assert pass_verdict(result) is not Verdict.VERIFIED


# -- SEC-229: the Claude Code reader and invalid UTF-8 ---------------------------

def _cc_lines():
    return [json.dumps(r).encode() for r in (
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        tool_result("t1", "3 passed in 0.1s"), assistant_text("All tests pass."))]


def test_invalid_utf8_line_is_read_past(tmp_path):
    lines = _cc_lines()
    lines.insert(1, b'{"type":"assistant","x":"\xff\xfe\xc0\xaf"}')
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"\n".join(lines) + b"\n")
    for agent in ("claude-code", "auto"):
        assert pass_verdict(audit(path, agent)) is Verdict.VERIFIED


def test_invalid_utf8_in_command_reads(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"\n".join(l.replace(b"pytest -q", b"pytest \xff-q") for l in _cc_lines()))
    audit(path, "claude-code")  # no UnicodeDecodeError


def test_live_session_cut_inside_a_character(tmp_path):
    cut = json.dumps(assistant_text("Done \u2714 now"), ensure_ascii=False).encode()
    at = cut.index(b"\xe2\x9c\x94")
    path = tmp_path / "live.jsonl"
    path.write_bytes(b"\n".join(_cc_lines()) + b"\n" + cut[:at + 2])
    result = audit(path, "claude-code")
    assert pass_verdict(result) is Verdict.VERIFIED
    assert result.session.unrecognised == 1


def test_damaged_database_in_auto_mode_is_a_plain_message(tmp_path, capsys):
    path = tmp_path / "sessions.db"
    path.write_bytes(b"SQLite format 3\x00" + b"\xff\xfe\x80" * 4000)
    code = cli.main(["audit", str(path), "--no-disk-check"])
    err = capsys.readouterr().err
    assert code == 2 and "Traceback" not in err
