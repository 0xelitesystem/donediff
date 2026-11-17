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


# -- SEC-230: SQLite size caps -------------------------------------------------

def _goose_generated(tmp_path, column_sql: str) -> str:
    import sqlite3
    db = tmp_path / "g.db"
    conn = sqlite3.connect(db)
    for statement in mgo.SCHEMA[:2]:
        conn.execute(statement)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT, "
                 "session_id TEXT NOT NULL, role TEXT NOT NULL, content_json TEXT NOT NULL, "
                 "created_timestamp INTEGER NOT NULL, tokens INTEGER, " + column_sql + ")")
    conn.execute("INSERT INTO schema_version (version) VALUES (18)")
    conn.execute("INSERT INTO sessions (id, working_dir) VALUES ('20260610_1', '/home/dev/x')")
    conn.execute("INSERT INTO messages (session_id, role, content_json, created_timestamp) "
                 "VALUES ('20260610_1', 'assistant', '[]', 1781092800)")
    conn.commit()
    conn.close()
    return str(db)


@pytest.mark.parametrize("kind", ["VIRTUAL", "STORED"])
def test_goose_generated_column_is_refused(tmp_path, kind):
    from donediff.readers.base import UnreadableSession
    db = _goose_generated(tmp_path, "metadata_json TEXT GENERATED ALWAYS AS "
                                    f"(char(0) || printf('%.*c', 64, 'x')) {kind}")
    with pytest.raises(UnreadableSession, match="computed"):
        audit(db, "goose")


def test_goose_nul_led_metadata_is_capped_in_bytes(tmp_path):
    from donediff.readers import goose as gs
    b = mgo.Builder(tmp_path / "g.db").session("20260610_1")
    b.user("Run tests.")
    b.shell("pytest -q", "3 passed", 0)
    b.say("All tests pass.")
    b.conn.execute("UPDATE messages SET metadata_json = ? WHERE id = 1",
                   ("\x00" + "x" * (gs._MAX_META + 10),))
    result = audit(b.close(), "goose")
    assert result.session.unrecognised >= 1  # the metadata was not fetched


def _opencode_db(tmp_path):
    o = mo.Builder(tmp_path / "oc.db", "v1").session("ses_t0001")
    o.user("Run tests.")
    o.shell("pytest -q", "3 passed", 0)
    o.say("All tests pass.")
    return o


def test_opencode_generated_column_is_refused(tmp_path):
    import sqlite3
    from donediff.readers.base import UnreadableSession
    db = tmp_path / "o.db"
    conn = sqlite3.connect(db)
    conn.executescript(mo._V1_SQL.replace(
        "time_updated INTEGER NOT NULL, data TEXT NOT NULL);\nCREATE TABLE part",
        "time_updated INTEGER NOT NULL, data TEXT NOT NULL);\nCREATE TABLE part", 1))
    conn.execute("DROP TABLE part")
    conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, "
                 "session_id TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER "
                 "NOT NULL, data TEXT GENERATED ALWAYS AS (printf('%.*c', 64, 'x')) VIRTUAL)")
    conn.execute("INSERT INTO session VALUES ('ses_t0001','p','s','/home/dev/x','t','1.0',"
                 "1781092800000,1781092800000)")
    conn.commit()
    conn.close()
    from donediff.readers import opencode as oc
    with pytest.raises(UnreadableSession, match="computed"):
        with oc._connect(db):
            pass
    # The listing treats it as unreadable: no session, nothing computed.
    with pytest.raises(FileNotFoundError):
        audit(f"{db}#ses_t0001", "opencode")


def test_opencode_row_over_cap_is_skipped(tmp_path, monkeypatch):
    from donediff.readers import opencode as oc
    o = _opencode_db(tmp_path)
    db = o.close()
    monkeypatch.setattr(oc, "_CAPPED", "CASE WHEN LENGTH(CAST(data AS BLOB)) > 40 "
                                       "THEN NULL ELSE data END")
    result = audit(db, "opencode")
    assert result.session.unrecognised >= 1


def test_opencode_session_over_budget_is_refused(tmp_path, monkeypatch):
    from donediff.readers import opencode as oc
    from donediff.readers.base import UnreadableSession
    db = _opencode_db(tmp_path).close()
    monkeypatch.setattr(oc, "_MAX_TOTAL", 50)
    with pytest.raises(UnreadableSession, match="larger than donediff reads"):
        audit(db, "opencode")
    monkeypatch.setattr(oc, "_MAX_TOTAL", 1 << 30)
    monkeypatch.setattr(oc, "_MAX_ROWS", 2)
    with pytest.raises(UnreadableSession, match="larger than donediff reads"):
        audit(db, "opencode")


# -- SEC-231: line and file caps in the Claude Code readers ----------------------

def _cc_with_padded_line(tmp_path, name="s.jsonl"):
    padded = json.dumps({"type": "progress", "pad": [{}] * 400})
    path = tmp_path / name
    path.write_text("\n".join([padded] + [l.decode() for l in _cc_lines()]) + "\n")
    return path


def test_main_line_over_cap_is_skipped_and_counted(tmp_path, monkeypatch):
    from donediff import parser
    path = _cc_with_padded_line(tmp_path)
    monkeypatch.setattr(parser, "MAX_LINE", 1000)
    result = audit(path, "claude-code")
    assert result.session.unrecognised == 1
    assert pass_verdict(result) is Verdict.VERIFIED


def test_main_file_over_cap_is_refused(tmp_path, monkeypatch):
    from donediff import parser
    from donediff.readers.base import UnreadableSession
    path = _cc_with_padded_line(tmp_path)
    monkeypatch.setattr(parser, "MAX_FILE", 100)
    with pytest.raises(UnreadableSession, match="larger than donediff reads"):
        audit(path, "claude-code")


def _with_subagent(tmp_path):
    main = write_jsonl(tmp_path / "m.jsonl", [assistant_text("Started.")])
    sub = tmp_path / "m" / "subagents"
    sub.mkdir(parents=True)
    padded = json.dumps({"type": "progress", "pad": [{}] * 400})
    (sub / "agent-a1.jsonl").write_text(
        padded + "\n" + "\n".join(l.decode() for l in _cc_lines()) + "\n")
    return main


def test_subagent_line_over_cap_is_skipped_and_counted(tmp_path, monkeypatch):
    from donediff import delegated
    main = _with_subagent(tmp_path)
    monkeypatch.setattr(delegated, "MAX_LINE", 1000)
    result = audit(main, "claude-code")
    assert result.session.unrecognised == 1


def test_subagent_file_over_cap_is_not_read(tmp_path, monkeypatch):
    from donediff import delegated
    main = _with_subagent(tmp_path)
    monkeypatch.setattr(delegated, "MAX_FILE", 100)
    result = audit(main, "claude-code")
    assert result.session.unrecognised == 1
    assert all(not s.events for s in result.session.sources)


# -- SEC-241: byte caps are not allocation caps ---------------------------------

def test_too_many_values_counts_outside_strings():
    from donediff.parser import MAX_VALUES, too_many_values
    bomb = "[" + "{}," * (MAX_VALUES + 10) + "{}]"
    assert too_many_values(bomb) and too_many_values(bomb.encode())
    prose = json.dumps({"text": "a, b, [c], {d}, \\\"e\\\", " * 200_000})
    assert len(prose) > 1024 * 1024 and not too_many_values(prose)


def test_loads_refuses_a_value_bomb_as_value_error():
    from donediff.parser import MAX_VALUES, loads
    with pytest.raises(ValueError):
        loads("[" + "{}," * (MAX_VALUES + 10) + "{}]")


def _bomb_record(record: dict) -> str:
    from donediff.parser import MAX_VALUES
    text = json.dumps(record)
    return text[:-1] + ', "pad": [' + "{}," * (MAX_VALUES + 10) + "{}]}"


def test_cline_value_bomb_is_unreadable(tmp_path):
    from donediff.readers.base import UnreadableSession
    doc = ml.Doc()
    doc.user("x")
    doc.assistant("All tests pass.")
    path = tmp_path / f"{ml.SID}.messages.json"
    path.write_text(_bomb_record(doc.document()))
    with pytest.raises(UnreadableSession):
        audit(path, "cline")


def test_gemini_value_bomb_line_is_skipped(tmp_path):
    c = mg.Clock()
    path = tmp_path / "session-2026-06-10T12-00-6f1c2d3e.jsonl"
    path.write_text(json.dumps(mg.meta()) + "\n" + _bomb_record(mg.gemini(c, "g2", "Done.")) + "\n")
    result = audit(path, "gemini")
    assert result.session.unrecognised == 1


def test_claude_code_value_bomb_line_is_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    lines = [l.decode() for l in _cc_lines()]
    path.write_text(_bomb_record(json.loads(lines[0])) + "\n" + "\n".join(lines[1:]) + "\n")
    assert audit(path, "claude-code").session.unrecognised == 1
