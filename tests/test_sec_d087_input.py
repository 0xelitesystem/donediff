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


# -- SEC-240: a lone surrogate in log text and --md ------------------------------

def _surrogate_transcript(tmp_path):
    path = tmp_path / "surr.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (
        assistant_tool("t1", "Bash", {"command": "pytest -q"}),
        tool_result("t1", "3 passed in 0.1s"),
        assistant_text("All tests pass in \ud800."))), encoding="utf-8")
    return path


def test_surrogate_in_markdown_report_is_written(tmp_path, capsys):
    md = tmp_path / "r.md"
    code = cli.main(["audit", str(_surrogate_transcript(tmp_path)), "--no-disk-check",
                     "--md", str(md)])
    out = capsys.readouterr()
    assert code == 0 and "Traceback" not in out.err
    assert md.stat().st_size > 0 and "All tests pass in ?." in md.read_text(encoding="utf-8")
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".donediff-")] == []


def test_failed_markdown_write_leaves_no_file(tmp_path, monkeypatch, capsys):
    md = tmp_path / "r.md"

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli.os, "replace", boom)
    code = cli.main(["audit", str(_surrogate_transcript(tmp_path)), "--no-disk-check",
                     "--md", str(md)])
    assert code == 2 and "cannot write" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [tmp_path / "surr.jsonl"]


# -- SEC-242: the Goose deleted-gap check in one pass ----------------------------

def _interleaved_goose(tmp_path, n: int) -> str:
    """Two sessions whose rows alternate, "id" a plain column (no rowid index),
    one id missing near the start: every gap of session A needs a check."""
    import sqlite3
    db = tmp_path / "g.db"
    conn = sqlite3.connect(db)
    for statement in mgo.SCHEMA[:2]:
        conn.execute(statement)
    conn.execute("CREATE TABLE messages (id INT, message_id TEXT, session_id TEXT NOT NULL, "
                 "role TEXT NOT NULL, content_json TEXT NOT NULL, created_timestamp INTEGER "
                 "NOT NULL, tokens INTEGER, metadata_json TEXT)")
    conn.execute("INSERT INTO schema_version (version) VALUES (18)")
    conn.execute("INSERT INTO sessions (id, working_dir) VALUES ('A', '/home/dev/x')")
    conn.execute("INSERT INTO sessions (id, working_dir) VALUES ('B', '/home/dev/y')")
    text = json.dumps([{"type": "text", "text": "ok"}])
    rows = [(1, "B", text, 1781092800)]
    for i in range(1, n + 1):
        rows.append((2 * i, "A", text, 1781092800 + 2 * i))
        if 2 * i + 1 > 3 and i < n:
            rows.append((2 * i + 1, "B", text, 1781092800 + 2 * i + 1))
    conn.executemany("INSERT INTO messages (id, session_id, role, content_json, "
                     "created_timestamp) VALUES (?, ?, 'assistant', ?, ?)", rows)
    conn.commit()
    conn.close()
    return f"{db}#A"


def test_goose_gap_check_is_one_pass(tmp_path, monkeypatch):
    from donediff.readers import goose as gs
    statements: list[str] = []
    real = gs._connect

    class Traced:
        def __init__(self, db):
            self.inner = real(db)

        def __enter__(self):
            conn = self.inner.__enter__()
            conn.set_trace_callback(statements.append)
            return conn

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

    monkeypatch.setattr(gs, "_connect", Traced)
    audit(_interleaved_goose(tmp_path, 400), "goose")
    gap_counts = [s for s in statements if "id > " in s and "id < " in s]
    assert len(gap_counts) <= 2, len(gap_counts)


def test_goose_gap_check_still_finds_a_deleted_row():
    from donediff.readers import goose as gs
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE messages (id INT, session_id TEXT, created_timestamp INTEGER)")
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?)",
                     [(1, "A", 1), (2, "B", 2), (3, "A", 3), (5, "A", 5), (6, "B", 6), (7, "A", 7)])
    assert gs._last_deleted_gap(conn, [1, 3, 5, 7], "A") == 3  # id 4 is gone
    conn.execute("INSERT INTO messages VALUES (4, 'B', 4)")
    assert gs._last_deleted_gap(conn, [1, 3, 5, 7], "A") is None


# -- SEC-243: private copies left by a killed run --------------------------------

def _copy_folder(root: Path, name: str, files=("copy.db", "copy.db-wal"), age=0.0) -> Path:
    import os
    folder = root / name
    folder.mkdir()
    for f in files:
        (folder / f).write_bytes(b"x")
        os.utime(folder / f, (1_000_000 - age, 1_000_000 - age))
    os.utime(folder, (1_000_000 - age, 1_000_000 - age))
    return folder


def test_stale_copy_folders_are_swept_and_nothing_else(tmp_path):
    from donediff.readers import opencode as oc
    old = oc.STALE_COPY + 10
    stale = _copy_folder(tmp_path, "donediff-old1", age=old)
    fresh = _copy_folder(tmp_path, "donediff-new1", age=5)
    extra = _copy_folder(tmp_path, "donediff-mix1", files=("copy.db", "notes.txt"), age=old)
    other = _copy_folder(tmp_path, "something-old", age=old)
    empty = tmp_path / "donediff-empty"
    empty.mkdir()
    oc.sweep_stale_copies(str(tmp_path), now=1_000_000)
    assert not stale.exists()
    assert fresh.exists() and extra.exists() and other.exists() and empty.exists()


def test_first_private_copy_sweeps_once(tmp_path, monkeypatch):
    from donediff.readers import opencode as oc
    calls = []
    monkeypatch.setattr(oc, "_SWEPT", False)
    monkeypatch.setattr(oc, "sweep_stale_copies", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(oc.tempfile, "tempdir", str(tmp_path))
    for name in ("a", "b"):
        db = tmp_path / f"{name}.db"
        db.write_bytes(b"x")
        (tmp_path / f"{name}.db-wal").write_bytes(b"y")
        oc.private_copy(db, "copy failed")
    oc.drop_copies()
    assert calls == [1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_private_copy_folder_is_owner_only(tmp_path, monkeypatch):
    import subprocess
    from donediff.readers import opencode as oc
    shared = tmp_path / "shared"
    shared.mkdir()
    subprocess.run(["icacls", str(shared), "/grant", "*S-1-1-0:(OI)(CI)F"],
                   check=True, capture_output=True)
    monkeypatch.setattr(oc.tempfile, "tempdir", str(shared))
    db = tmp_path / "x.db"
    db.write_bytes(b"x")
    (tmp_path / "x.db-wal").write_bytes(b"y")
    copy = oc.private_copy(db, "copy failed")
    try:
        acl = subprocess.run(["icacls", str(copy.parent)], capture_output=True,
                             text=True).stdout
        assert "Everyone" not in acl and "S-1-1-0" not in acl
        assert "(I)" not in acl  # nothing inherited from the shared temp folder
    finally:
        oc.drop_copies()


# -- SEC-218 (A8): side files decide copy or direct, not the header ---------------

def _rollback_header(db: Path) -> None:
    data = bytearray(db.read_bytes())
    data[18] = data[19] = 1  # the header says rollback journal
    db.write_bytes(bytes(data))


@pytest.mark.parametrize("module, name", [("opencode", "opencode"), ("goose", "Goose")])
def test_rollback_header_with_side_files(tmp_path, module, name):
    import importlib
    from donediff.readers import opencode as oc
    from donediff.readers.base import UnreadableSession
    reader = importlib.import_module(f"donediff.readers.{module}")
    db = tmp_path / "x.db"
    db.write_bytes(b"SQLite format 3\x00" + bytes(84))
    _rollback_header(db)
    assert reader._open_mode(db) == "?mode=ro"
    (tmp_path / "x.db-wal").write_bytes(b"frames")
    with pytest.raises(UnreadableSession, match=f"{name} database has an unfinished write log"):
        reader._open_mode(db)  # a lone -wal: refused, as with a WAL header
    (tmp_path / "x.db-shm").write_bytes(b"index")
    assert reader._open_mode(db) == oc.COPY  # read from a private copy, never in place


def test_goose_rollback_header_with_live_wal_leaves_the_folder_untouched(tmp_path):
    import shutil
    import sqlite3
    from donediff.readers.base import UnreadableSession
    live = tmp_path / "live"
    live.mkdir()
    b = mgo.Builder(live / "sessions.db")
    b.conn.commit()
    b.conn.execute("PRAGMA journal_mode=WAL")
    b.session("20260610_1")
    b.user("Run tests.")
    b.shell("pytest -q", "3 passed", 0)
    b.say("All tests pass.")
    b.close()
    writer = sqlite3.connect(live / "sessions.db")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO messages (session_id, role, content_json, created_timestamp) "
                   "VALUES ('20260610_1', 'assistant', '[]', 1781099999)")
    writer.commit()
    copy = tmp_path / "copy"
    copy.mkdir()
    shutil.copyfile(live / "sessions.db", copy / "sessions.db")
    shutil.copyfile(live / "sessions.db-wal", copy / "sessions.db-wal")
    writer.close()
    _rollback_header(copy / "sessions.db")
    before = sorted(p.name for p in copy.iterdir())
    with pytest.raises(UnreadableSession):
        audit(f"{copy / 'sessions.db'}#20260610_1", "goose")
    assert sorted(p.name for p in copy.iterdir()) == before  # no -shm written
