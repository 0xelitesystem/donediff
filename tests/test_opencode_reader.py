"""The opencode reader: discovery, mapping to shared actions, verdicts, a read-only DB."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from donediff import cli
from donediff.models import EDIT, OTHER, SHELL, WRITE, ClaimType, Verdict
from donediff.readers import OPENCODE, select, select_for_list
from donediff.readers import opencode as oc
from donediff.readers.base import UnreadableSession
from donediff.readers.claude_code import CLAUDE_CODE
from donediff.readers.codex import CODEX

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo-session.jsonl"
_spec = importlib.util.spec_from_file_location(
    "make_opencode", ROOT / "tests" / "fixtures" / "opencode" / "make_opencode.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

SCHEMAS = ("v1", "v2")


def audit(target, **kw):
    return cli.run_audit(str(target), check_disk=False, agent=kw.pop("agent", "opencode"), **kw)


def by_type(result, claim_type):
    return [f for f in result.findings if f.claim.type is claim_type]


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def one_session(tmp_path, schema, build, sid="ses_t0001"):
    b = mk.Builder(tmp_path / f"{schema}.db", schema).session(sid)
    b.user("Please do the task.")
    build(b)
    return b.close()


# -- where and which ----------------------------------------------------------

def test_database_path_default_env_and_bad_values(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCODE_DB")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert oc.database_path() == tmp_path / "opencode" / "opencode.db"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert oc.database_path() == Path("~").expanduser() / ".local" / "share" / "opencode" / "opencode.db"
    monkeypatch.setenv("OPENCODE_DB", "custom.db")
    assert oc.database_path().name == "custom.db" and oc.database_path().parent.name == "opencode"
    for bad in (":memory:", "\\\\host\\share\\opencode.db", "//host/share/opencode.db"):
        monkeypatch.setenv("OPENCODE_DB", bad)
        assert oc.database_path() is None and OPENCODE.discover() == []


@pytest.mark.parametrize("schema", SCHEMAS)
def test_discover_newest_first_resolve_and_filter(tmp_path, monkeypatch, schema):
    db = tmp_path / "opencode.db"
    b = mk.Builder(db, schema)
    b.session("ses_old0001", "/home/dev/alpha", updated=mk.T0 + 1000)
    b.session("ses_new0002", "/home/dev/beta", updated=mk.T0 + 9000)
    b.close()
    monkeypatch.setenv("OPENCODE_DB", str(db))
    found = OPENCODE.discover()
    assert [p.session_id for p in found] == ["ses_new0002", "ses_old0001"]
    assert [p.session_id for p in OPENCODE.discover("alpha")] == ["ses_old0001"]
    assert OPENCODE.resolve("latest").session_id == "ses_new0002"
    assert OPENCODE.resolve("ses_old").session_id == "ses_old0001"
    assert OPENCODE.resolve(str(db)).session_id == "ses_new0002"
    assert OPENCODE.resolve(f"{db}#ses_old").session_id == "ses_old0001"
    with pytest.raises(FileNotFoundError):
        OPENCODE.resolve("ses_nope")
    assert OPENCODE.describe(found[1]) == ("ses_old0001", "alpha")


def test_detect_and_auto(tmp_path, monkeypatch):
    db = mk.demo(tmp_path / "opencode.db")
    assert OPENCODE.detect(Path(db)) and not CLAUDE_CODE.detect(Path(db)) and not CODEX.detect(Path(db))
    assert not OPENCODE.detect(DEMO)
    assert select("auto", db) is OPENCODE
    assert select("auto", str(DEMO)) is CLAUDE_CODE
    assert select("opencode") is OPENCODE
    monkeypatch.setenv("OPENCODE_DB", db)
    monkeypatch.setattr(CLAUDE_CODE, "discover", lambda project=None: [])
    assert select("auto", "latest") is OPENCODE
    assert select_for_list("auto") is OPENCODE
    assert select("auto", "ses_demo") is OPENCODE


def test_claude_code_stays_first_in_auto(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DB", mk.demo(tmp_path / "opencode.db"))
    monkeypatch.setattr(CLAUDE_CODE, "discover", lambda project=None: [DEMO])
    assert select("auto", "latest") is CLAUDE_CODE
    assert select_for_list("auto") is CLAUDE_CODE


# -- verdicts -----------------------------------------------------------------

@pytest.mark.parametrize("schema", SCHEMAS)
def test_demo_verified_with_real_exit_code(tmp_path, schema):
    result = audit(mk.demo(tmp_path / "o.db", schema))
    assert [(f.claim.type, f.verdict) for f in result.findings] == [
        (ClaimType.TESTS_PASS, Verdict.VERIFIED), (ClaimType.COMMIT_MADE, Verdict.VERIFIED)]
    run = [e for e in result.session.tool_calls() if e.tool_action == SHELL][0]
    assert run.exit_code == 0 and run.result_seen is True and run.cwd == mk.CWD
    assert "exit 0" in by_type(result, ClaimType.TESTS_PASS)[0].evidence


@pytest.mark.parametrize("schema", SCHEMAS)
def test_failed_run_is_contradicted(tmp_path, schema):
    db = one_session(tmp_path, schema, lambda b: (
        b.shell("pytest -q", "3 failed, 9 passed", 1), b.say("All tests pass.")))
    finding = by_type(audit(db), ClaimType.TESTS_PASS)[0]
    assert finding.verdict is Verdict.CONTRADICTED and "exit 1" in finding.evidence


@pytest.mark.parametrize("schema", SCHEMAS)
def test_status_only_is_unverified_and_says_status(tmp_path, schema):
    db = one_session(tmp_path, schema, lambda b: (
        b.shell("pytest -q", "12 passed", exit=None), b.say("All tests pass.")))
    result = audit(db)
    run = [e for e in result.session.tool_calls() if e.tool_action == SHELL][0]
    assert run.exit_code is None and run.status == "completed" and run.result_seen is True
    finding = by_type(result, ClaimType.TESTS_PASS)[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "the log has a status only (completed)" in finding.evidence
    assert "exit 0" not in finding.evidence and "exit code" not in finding.evidence


@pytest.mark.parametrize("schema", SCHEMAS)
def test_error_state_without_exit_is_contradicted_by_status(tmp_path, schema):
    db = one_session(tmp_path, schema, lambda b: (
        b.tool(mk.SHELL_TOOL[schema], {"command": "pytest -q"}, "error", error="aborted"),
        b.say("All tests pass.")))
    finding = by_type(audit(db), ClaimType.TESTS_PASS)[0]
    assert finding.verdict is Verdict.CONTRADICTED and "status error" in finding.evidence
    assert "exit" not in finding.evidence.split("(")[1].split(")")[0]


@pytest.mark.parametrize("schema", SCHEMAS)
@pytest.mark.parametrize("status", ["pending", "running"])
def test_call_with_no_result_is_unverified(tmp_path, schema, status):
    status = "streaming" if (schema, status) == ("v2", "pending") else status
    db = one_session(tmp_path, schema, lambda b: (
        b.shell("pytest -q", status=status), b.say("All tests pass.")))
    result = audit(db)
    run = [e for e in result.session.tool_calls() if e.tool_action == SHELL][0]
    assert run.result_seen is False and run.exit_code is None
    finding = by_type(result, ClaimType.TESTS_PASS)[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "no result for this run in the log" in finding.evidence


def test_background_shell_is_no_evidence(tmp_path):
    db = one_session(tmp_path, "v2", lambda b: (
        b.tool("shell", {"command": "pytest -q"}, output="started",
               metadata={"status": "running"}),
        b.say("All tests pass.")))
    result = audit(db)
    run = result.session.tool_calls()[0]
    assert run.background and run.status == "running" and run.exit_code is None
    assert by_type(result, ClaimType.TESTS_PASS)[0].verdict is Verdict.UNVERIFIED


def test_user_shell_message_carries_its_exit(tmp_path):
    db = one_session(tmp_path, "v2", lambda b: (
        b.user_shell("pytest -q", "exited", 2, "2 failed"), b.say("All tests pass.")))
    result = audit(db)
    run = result.session.tool_calls()[0]
    assert run.tool_action == SHELL and run.exit_code == 2 and run.result_seen
    assert by_type(result, ClaimType.TESTS_PASS)[0].verdict is Verdict.CONTRADICTED


@pytest.mark.parametrize("schema", SCHEMAS)
def test_write_edit_and_patch_mapping(tmp_path, schema):
    patch = ("*** Begin Patch\n*** Add File: src/new.py\n+x = 1\n"
             "*** Update File: src/old.py\n@@\n-a = 1\n+a = 2\n*** End Patch\n")
    db = one_session(tmp_path, schema, lambda b: (
        b.write("/home/dev/acme-api/a.py", "print(1)\n"),
        b.edit("/home/dev/acme-api/b.py", "x", "y"),
        b.tool(mk.PATCH_TOOL[schema], {"patchText": patch}, output="done"),
        b.tool("read", {"filePath": "/home/dev/acme-api/a.py"}, output="print(1)")))
    calls = OPENCODE.parse(OPENCODE.resolve(db)).tool_calls()
    assert [(e.tool_action, e.file_path) for e in calls] == [
        (WRITE, "/home/dev/acme-api/a.py"), (EDIT, "/home/dev/acme-api/b.py"),
        (WRITE, "src/new.py"), (EDIT, "src/old.py"), (OTHER, "")]
    assert calls[0].tool_input["content"] == "print(1)\n"
    assert calls[1].tool_input["old_string"] == "x" and calls[1].tool_input["new_string"] == "y"
    assert calls[3].tool_input["new_string"] == "a = 2"
    assert all(e.result_seen for e in calls)


@pytest.mark.parametrize("schema", SCHEMAS)
def test_unknown_parts_and_bad_json_are_counted(tmp_path, schema):
    def build(b):
        b.raw_part({"type": "hologram", "text": "?"})
        b.say("Hello.")
        b._part(b._assistant(), {"type": "text", "text": "x"})
        if schema == "v2":
            b.raw_message("assistant", "{not json")
            b.raw_message("teleport", "{}")
        else:
            b.conn.execute("UPDATE part SET data = '{broken' WHERE data LIKE '%\"x\"%'")
    session = OPENCODE.parse(OPENCODE.resolve(one_session(tmp_path, schema, build)))
    assert session.unrecognised == (3 if schema == "v2" else 2)
    assert [e.text for e in session.text_events()] == ["Hello."] + (["x"] if schema == "v2" else [])


def test_prompt_starts_and_text_order(tmp_path):
    db = one_session(tmp_path, "v1", lambda b: (b.say("One."), b.user("Again."), b.say("Two.")))
    session = OPENCODE.parse(OPENCODE.resolve(db))
    assert [e.text for e in session.text_events()] == ["One.", "Two."]
    assert session.prompt_starts == [0, 1]
    assert session.first_timestamp.startswith("2026-06-10T12:00:")


# -- the database is only read ------------------------------------------------

def test_missing_db_and_missing_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "absent.db"))
    assert OPENCODE.discover() == []
    with pytest.raises(FileNotFoundError):
        OPENCODE.resolve("latest")
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).execute("CREATE TABLE other (x)").connection.close()
    assert not OPENCODE.detect(empty)
    with pytest.raises(FileNotFoundError):
        OPENCODE.resolve(str(empty))


def test_corrupt_db_is_a_clean_error(tmp_path, capsys):
    bad = tmp_path / "opencode.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
    assert not OPENCODE.detect(bad)
    assert OPENCODE._list(bad) == []
    path = oc.session_path(bad, "ses_x", "v2")
    with pytest.raises(UnreadableSession):
        OPENCODE.parse(path)
    assert cli.main(["audit", f"{bad}#ses_x", "--agent", "opencode"]) == 2
    assert "donediff:" in capsys.readouterr().err


def test_locked_db_is_a_clean_error(tmp_path, monkeypatch):
    db = mk.demo(tmp_path / "opencode.db")
    path = OPENCODE.resolve(db)
    monkeypatch.setattr(oc, "_TIMEOUT", 0.05)
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(UnreadableSession, match="locked"):
            OPENCODE.parse(path)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert OPENCODE.parse(path).tool_calls()


@pytest.mark.parametrize("schema", SCHEMAS)
def test_db_is_byte_unchanged_after_a_read(tmp_path, monkeypatch, schema, capsys):
    db = mk.demo(tmp_path / "opencode.db", schema)
    before = sha(db)
    files_before = sorted(p.name for p in tmp_path.iterdir())
    monkeypatch.setenv("OPENCODE_DB", db)
    audit(db)
    OPENCODE.detect(Path(db))
    assert cli.main(["list", "--agent", "opencode"]) == 0
    out = capsys.readouterr().out
    assert "ses_demo0001" in out and "acme-api" in out
    assert sha(db) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before


def test_read_only_connection_refuses_writes(tmp_path):
    db = mk.demo(tmp_path / "opencode.db")
    with oc._connect(Path(db)) as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM session_v2")


def test_session_path_stats_as_the_session(tmp_path):
    db = mk.demo(tmp_path / "opencode.db")
    path = OPENCODE.resolve(db)
    assert path.stat().st_size > 0 and path.stat().st_size < Path(db).stat().st_size
    assert path.stat().st_mtime == pytest.approx((mk.T0 + 1000) / 1000)
    result = audit(db)
    assert result.session.path.endswith("#ses_demo0001")
    assert result.session.scan.bytes == path.stat().st_size


# -- SEC-203 / SEC-204 ----------------------------------------------------------

@pytest.mark.parametrize("xdg", ["\\\\srv\\share", "//srv/share"])
def test_relative_db_on_a_remote_data_home_is_never_used(monkeypatch, xdg):
    # SEC-203: the final joined path is checked, not only the two parts.
    monkeypatch.setenv("XDG_DATA_HOME", xdg)
    monkeypatch.setenv("OPENCODE_DB", "opencode.db")
    assert oc.database_path() is None
    monkeypatch.delenv("OPENCODE_DB")
    assert oc.database_path() is None


def _to_wal(db) -> None:
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    conn.close()  # the last close checkpoints and removes -wal and -shm


@pytest.mark.parametrize("schema", SCHEMAS)
def test_wal_db_with_no_side_files_gets_none_created(tmp_path, monkeypatch, schema, capsys):
    # SEC-204: opencode runs WAL; a read must not create -shm or -wal beside it.
    db = mk.demo(tmp_path / "opencode.db", schema)
    _to_wal(db)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["opencode.db"]
    assert oc._open_mode(Path(db)) == "?mode=ro&immutable=1"
    before = sha(db)
    monkeypatch.setenv("OPENCODE_DB", db)
    assert audit(db).session.tool_calls()
    OPENCODE.detect(Path(db))
    assert cli.main(["list", "--agent", "opencode"]) == 0
    assert "ses_demo0001" in capsys.readouterr().out
    assert sha(db) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["opencode.db"]


def test_wal_db_with_a_lone_wal_file_is_refused_and_nothing_created(tmp_path):
    db = mk.demo(tmp_path / "opencode.db")
    _to_wal(db)
    (tmp_path / "opencode.db-wal").write_bytes(b"leftover")
    with pytest.raises(UnreadableSession):
        oc._open_mode(Path(db))
    with pytest.raises(UnreadableSession):
        OPENCODE.parse(oc.session_path(Path(db), "ses_demo0001", "v2"))
    with pytest.raises(FileNotFoundError):  # list/resolve skip it: nothing to audit
        OPENCODE.resolve(db)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["opencode.db", "opencode.db-wal"]


def test_wal_db_with_both_side_files_reads_them(tmp_path):
    db = mk.demo(tmp_path / "opencode.db")
    _to_wal(db)
    (tmp_path / "opencode.db-wal").write_bytes(b"")
    (tmp_path / "opencode.db-shm").write_bytes(b"")
    assert oc._open_mode(Path(db)) == "?mode=ro"


def test_rollback_journal_db_opens_plain_read_only(tmp_path):
    db = mk.demo(tmp_path / "opencode.db")
    assert oc._open_mode(Path(db)) == "?mode=ro"
