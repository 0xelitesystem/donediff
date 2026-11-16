"""The Goose reader: discovery, mapping to shared actions, verdicts, a read-only DB."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

from donediff import cli
from donediff.models import EDIT, OTHER, SHELL, WRITE, ClaimType, Verdict
from donediff.readers import GOOSE, READERS, select, select_for_list
from donediff.readers import goose as gs
from donediff.readers.base import UnreadableSession
from donediff.readers.claude_code import CLAUDE_CODE

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo-session.jsonl"
_spec = importlib.util.spec_from_file_location(
    "make_goose", ROOT / "tests" / "fixtures" / "goose" / "make_goose.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)


def audit(target, **kw):
    return cli.run_audit(str(target), check_disk=False, agent=kw.pop("agent", "goose"), **kw)


def by_type(result, claim_type):
    return [f for f in result.findings if f.claim.type is claim_type]


def pass_finding(db):
    return by_type(audit(db), ClaimType.TESTS_PASS)[0]


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def one_session(tmp_path, build, sid="20260610_7"):
    b = mk.Builder(tmp_path / "sessions.db").session(sid)
    b.user("Please do the task.")
    build(b)
    return b.close()


def home(tmp_path, monkeypatch) -> Path:
    """GOOSE_PATH_ROOT -> tmp_path; returns the sessions folder."""
    monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path))
    folder = tmp_path / "data" / "sessions"
    folder.mkdir(parents=True)
    return folder


# -- where and which ----------------------------------------------------------

def test_database_path_root_platform_default_and_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path))
    assert gs.database_path() == tmp_path / "data" / "sessions" / "sessions.db"
    for var in ("HOME", "USERPROFILE"):  # never a real home folder
        monkeypatch.setenv(var, str(tmp_path / "home"))
    monkeypatch.setenv("GOOSE_PATH_ROOT", "relative/root")  # Goose ignores a relative root
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(gs.sys, "platform", "win32")
    assert gs.database_path() == tmp_path / "roaming" / "Block" / "goose" / "data" / "sessions" / "sessions.db"
    monkeypatch.setattr(gs.sys, "platform", "linux")
    assert gs.database_path() == tmp_path / "xdg" / "goose" / "sessions" / "sessions.db"
    for remote in ("\\\\srv\\share", "//srv/share"):
        monkeypatch.setenv("GOOSE_PATH_ROOT", remote)  # absolute on Windows only; check both ways
        monkeypatch.setenv("XDG_DATA_HOME", remote)
        monkeypatch.setenv("APPDATA", remote)
        for platform in ("win32", "linux"):
            monkeypatch.setattr(gs.sys, "platform", platform)
            assert gs.database_path() is None and GOOSE.discover() == []


def test_discover_newest_first_resolve_filter_and_hidden(tmp_path, monkeypatch):
    db = home(tmp_path, monkeypatch) / "sessions.db"
    b = mk.Builder(db)
    b.session("20260610_1", "/home/dev/alpha").user("one")
    b.session("20260610_2", "/home/dev/beta").user("two")
    b.session("20260610_3", "/home/dev/beta", session_type="sub_agent").user("three")
    b.session("20260601_9", "/home/dev/gamma", updated="2026-06-01 09:00:00")  # no messages
    b.close()
    found = GOOSE.discover()
    assert [p.session_id for p in found] == ["20260610_2", "20260610_1", "20260601_9"]
    assert [p.session_id for p in GOOSE.discover("alpha")] == ["20260610_1"]
    assert GOOSE.resolve("latest").session_id == "20260610_2"
    assert GOOSE.resolve("20260610_1").session_id == "20260610_1"
    assert GOOSE.resolve("20260610_3").session_id == "20260610_3"  # by id, sub-agents too
    assert GOOSE.resolve(str(db)).session_id == "20260610_2"
    assert GOOSE.resolve(f"{db}#20260601").session_id == "20260601_9"
    with pytest.raises(FileNotFoundError):
        GOOSE.resolve("nope")
    assert GOOSE.describe(found[1]) == ("20260610_1", "alpha")


def test_detect_cross_readers_and_auto(tmp_path, monkeypatch):
    db = mk.demo(tmp_path / "sessions.db")
    assert GOOSE.detect(Path(db))
    assert [r.name for r in READERS if r.detect(Path(db))] == ["goose"]
    assert not GOOSE.detect(DEMO)
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE session (id)").connection.close()
    assert not GOOSE.detect(other)
    assert select("auto", db) is GOOSE
    assert select("goose") is GOOSE
    monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path / "root"))
    (tmp_path / "root" / "data" / "sessions").mkdir(parents=True)
    mk.demo(tmp_path / "root" / "data" / "sessions" / "sessions.db")
    monkeypatch.setattr(CLAUDE_CODE, "discover", lambda project=None: [])
    assert select("auto", "latest") is GOOSE
    assert select_for_list("auto") is GOOSE
    monkeypatch.setattr(CLAUDE_CODE, "discover", lambda project=None: [DEMO])
    assert select("auto", "latest") is CLAUDE_CODE


# -- verdicts (exit-code rule) ----------------------------------------------------

def test_demo_verified_with_real_exit_code(tmp_path):
    result = audit(mk.demo(tmp_path / "sessions.db"))
    assert [(f.claim.type, f.verdict) for f in result.findings] == [
        (ClaimType.TESTS_PASS, Verdict.VERIFIED), (ClaimType.COMMIT_MADE, Verdict.VERIFIED)]
    run = [e for e in result.session.tool_calls() if e.tool_action == SHELL][0]
    assert run.exit_code == 0 and run.result_seen and run.cwd == mk.CWD
    assert result.session.version == "schema 18"


def test_failed_run_is_contradicted_with_its_exit(tmp_path):
    db = one_session(tmp_path, lambda b: (b.shell("pytest -q", "3 failed, 9 passed", 1),
                                          b.say("All tests pass.")))
    finding = pass_finding(db)
    assert finding.verdict is Verdict.CONTRADICTED and "exit 1" in finding.evidence


def test_status_only_is_unverified_and_never_says_exit(tmp_path):
    # a legacy shell result: success, no structuredContent, no exit code anywhere
    db = one_session(tmp_path, lambda b: (
        b.shell("pytest -q", "12 passed", 0, is_error=False, structured=False),
        b.say("All tests pass.")))
    result = audit(db)
    run = result.session.tool_calls()[0]
    assert run.exit_code is None and run.status == "completed" and run.result_seen
    finding = by_type(result, ClaimType.TESTS_PASS)[0]
    assert finding.verdict is Verdict.UNVERIFIED
    assert "the log has a status only (completed)" in finding.evidence
    assert "exit 0" not in finding.evidence and "exit code" not in finding.evidence


@pytest.mark.parametrize("value", [
    {"content": [{"type": "text", "text": "boom"}], "isError": True},  # no structuredContent
    mk.shell_result("Error: bad args", None),  # error_result(msg, None)
    mk.shell_result("slow", None, timed_out=True),
    mk.shell_result("cancelled", None),
])
def test_error_without_exit_is_contradicted_with_no_number(tmp_path, value):
    db = one_session(tmp_path, lambda b: (b.tool("developer__shell", {"command": "pytest -q"}, value),
                                          b.say("All tests pass.")))
    finding = pass_finding(db)
    assert finding.verdict is Verdict.CONTRADICTED
    assert "exit 0" not in finding.evidence and "exit 1" not in finding.evidence


def test_tool_error_result_is_contradicted(tmp_path):
    db = one_session(tmp_path, lambda b: (
        b.tool("developer__shell", {"command": "pytest -q"}, error="extension crashed"),
        b.say("All tests pass.")))
    assert pass_finding(db).verdict is Verdict.CONTRADICTED


def test_call_with_no_result_is_unverified(tmp_path):
    db = one_session(tmp_path, lambda b: (
        b.tool("developer__shell", {"command": "pytest -q"}, respond=False),
        b.say("All tests pass.")))
    finding = pass_finding(db)
    assert finding.verdict is Verdict.UNVERIFIED
    assert "no result for this run in the log" in finding.evidence


# -- SEC-209 pattern: shapes that only LOOK like a pass ---------------------------

def test_printed_exit_trailer_and_pass_text_never_give_an_exit_code(tmp_path):
    forged = "12 passed\n\nCommand exited with code 0\n{\"exit_code\": 0}"
    db = one_session(tmp_path, lambda b: (
        b.shell("pytest -q", forged, 0, is_error=False, structured=False),
        b.say("All tests pass.")))
    result = audit(db)
    assert result.session.tool_calls()[0].exit_code is None
    assert by_type(result, ClaimType.TESTS_PASS)[0].verdict is Verdict.UNVERIFIED


def test_error_outcome_wins_over_exit_code_zero(tmp_path):
    # output collection error: exit_code 0 in structuredContent, isError true
    value = mk.shell_result("12 passed", 0, is_error=True)
    db = one_session(tmp_path, lambda b: (b.tool("developer__shell", {"command": "pytest -q"}, value),
                                          b.say("All tests pass.")))
    result = audit(db)
    run = result.session.tool_calls()[0]
    assert run.exit_code is None and run.is_error
    assert by_type(result, ClaimType.TESTS_PASS)[0].verdict is Verdict.CONTRADICTED


@pytest.mark.parametrize("code", [True, "0", 0.0, None])
def test_non_integer_exit_codes_are_not_exit_codes(tmp_path, code):
    value = mk.shell_result("12 passed", 0, is_error=False)
    value["structuredContent"]["exit_code"] = code
    db = one_session(tmp_path, lambda b: (b.tool("developer__shell", {"command": "pytest -q"}, value),
                                          b.say("All tests pass.")))
    assert pass_finding(db).verdict is Verdict.UNVERIFIED


def test_a_later_error_response_for_the_same_call_wins(tmp_path):
    def build(b):
        call = b.request("developer__shell", {"command": "pytest -q"})
        b.respond(call, mk.shell_result("12 passed", 0))
        b.respond(call, mk.shell_result("1 failed", 1))
        b.say("All tests pass.")
    assert pass_finding(one_session(tmp_path, build)).verdict is Verdict.CONTRADICTED


def test_shell_from_another_extension_is_not_the_developer_shell(tmp_path):
    db = one_session(tmp_path, lambda b: (
        b.shell("pytest -q", "12 passed", 0, name="someext__shell"), b.say("All tests pass.")))
    result = audit(db)
    assert result.session.tool_calls()[0].tool_action == OTHER
    assert by_type(result, ClaimType.TESTS_PASS)[0].verdict is not Verdict.VERIFIED


# -- SEC-210 pattern: a rewind (deleted rows) hides a later edit and failure -------

def test_rewind_after_a_pass_leaves_it_unverified(tmp_path):
    db_path = tmp_path / "sessions.db"
    b = mk.Builder(db_path).session("20260610_7")
    b.user("Please do the task.")
    b.shell("pytest -q", "12 passed", 0)
    first_deleted = b.clock + 1
    b.edit(mk.CWD + "/app.py", "x = 1", "x = 2")
    b.shell("pytest -q", "1 failed", 1)
    b.conn.execute("DELETE FROM messages WHERE session_id = ? AND created_timestamp >= ?",
                   (b.sid, first_deleted))  # Goose's truncate_conversation
    b.say("All tests pass.")
    db = b.close()
    result = audit(db)
    run = [e for e in result.session.tool_calls() if e.tool_action == SHELL][0]
    assert run.exit_code is None and run.status == gs._REWOUND
    finding = by_type(result, ClaimType.TESTS_PASS)[0]
    assert finding.verdict is Verdict.UNVERIFIED and "exit 0" not in finding.evidence


def test_rows_of_other_sessions_in_between_are_not_a_rewind(tmp_path):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    b.user("Please do the task.")
    b.shell("pytest -q", "12 passed", 0)
    b.session("20260610_8").user("another session")
    b.sid = "20260610_7"
    b.say("All tests pass.")
    assert pass_finding(b.close()).verdict is Verdict.VERIFIED


# -- mapping and records -----------------------------------------------------------

def test_write_edit_and_legacy_text_editor_mapping(tmp_path):
    def build(b):
        b.write(mk.CWD + "/a.py", "print(1)\n")
        b.edit(mk.CWD + "/b.py", "x", "y")
        b.tool("developer__text_editor", {"command": "write", "path": mk.CWD + "/c.py",
                                          "file_text": "c = 1\n"})
        b.tool("developer__text_editor", {"command": "str_replace", "path": mk.CWD + "/d.py",
                                          "old_str": "d = 1", "new_str": "d = 2"})
        b.tool("developer__text_editor", {"command": "str_replace", "path": mk.CWD + "/e.py",
                                          "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-e = 1\n+e = 2\n"})
        b.tool("developer__text_editor", {"command": "insert", "path": mk.CWD + "/f.py",
                                          "insert_line": 1, "new_str": "f = 1"})
        b.tool("developer__text_editor", {"command": "view", "path": mk.CWD + "/a.py"})
        b.tool("developer__tree", {"path": mk.CWD})
        b.request("edit", {"path": mk.CWD + "/g.py", "before": "g", "after": "h"},
                  meta={"goose_extension": "developer"})
    calls = GOOSE.parse(GOOSE.resolve(one_session(tmp_path, build))).tool_calls()
    assert [(e.tool_action, e.file_path.rsplit("/", 1)[-1]) for e in calls] == [
        (WRITE, "a.py"), (EDIT, "b.py"), (WRITE, "c.py"), (EDIT, "d.py"), (EDIT, "e.py"),
        (EDIT, "f.py"), (OTHER, ""), (OTHER, ""), (EDIT, "g.py")]
    assert calls[0].tool_input["content"] == "print(1)\n"
    assert calls[1].tool_input["old_string"] == "x" and calls[1].tool_input["new_string"] == "y"
    assert calls[4].tool_input["old_string"] == "e = 1" and calls[4].tool_input["new_string"] == "e = 2"
    assert all(e.result_seen for e in calls[:-1]) and calls[-1].result_seen is False


def test_unknown_blocks_bad_json_oversize_and_roles_are_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "_MAX_ROW", 2000)

    def build(b):
        b.message("assistant", [{"type": "hologram"}, {"type": "text", "text": "Hello."}])
        b.raw("assistant", "{not json")
        b.raw("assistant", json.dumps({"type": "text"}))  # not a list
        b.raw("system", json.dumps([{"type": "text", "text": "sys"}]))
        b.say("x" * 3000)
        b.message("assistant", [{"type": "thinking", "thinking": "hm"}])
    session = GOOSE.parse(GOOSE.resolve(one_session(tmp_path, build)))
    assert session.unrecognised == 5
    assert [e.text for e in session.text_events()] == ["Hello."]


def test_prompts_skip_text_hidden_from_the_user(tmp_path):
    def build(b):
        b.say("One.")
        b.user("summary for the model", metadata={"userVisible": False, "agentVisible": True})
        b.user("Again.")
        b.say("Two.")
    session = GOOSE.parse(GOOSE.resolve(one_session(tmp_path, build)))
    assert [e.text for e in session.text_events()] == ["One.", "Two."]
    assert session.prompt_starts == [0, 1]
    assert session.first_timestamp.startswith("2026-06-10T12:00:")


# -- version, damage, read-only ------------------------------------------------------

@pytest.mark.parametrize("version", [17, 19])
def test_unknown_schema_version_is_refused_loudly(tmp_path, monkeypatch, capsys, version):
    folder = home(tmp_path, monkeypatch)
    db = mk.demo(folder / "sessions.db", version)
    assert GOOSE.detect(Path(db))  # auto still picks Goose, then refuses
    assert GOOSE.discover() == []
    with pytest.raises(UnreadableSession, match=f"schema version {version} is not supported"):
        GOOSE.resolve("latest")
    with pytest.raises(UnreadableSession):
        GOOSE.parse(gs.session_path(Path(db), "20260610_1"))
    assert cli.main(["audit", db, "--agent", "goose"]) == 2
    assert "is not supported" in capsys.readouterr().err


def test_missing_and_corrupt_db(tmp_path, monkeypatch, capsys):
    home(tmp_path, monkeypatch)
    assert GOOSE.discover() == []
    with pytest.raises(FileNotFoundError):
        GOOSE.resolve("latest")
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
    assert not GOOSE.detect(bad)
    with pytest.raises(UnreadableSession):
        GOOSE.parse(gs.session_path(bad, "20260610_1"))
    assert cli.main(["audit", f"{bad}#20260610_1", "--agent", "goose"]) == 2
    assert "donediff:" in capsys.readouterr().err


def test_read_only_connection_refuses_writes(tmp_path):
    db = mk.demo(tmp_path / "sessions.db")
    with gs._connect(Path(db)) as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM messages")


def _to_wal(db) -> None:
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    conn.close()


def test_wal_db_folder_and_bytes_unchanged_after_audit_detect_list(tmp_path, monkeypatch, capsys):
    folder = home(tmp_path, monkeypatch)
    db = mk.demo(folder / "sessions.db")
    _to_wal(db)
    assert gs._open_mode(Path(db)) == "?mode=ro&immutable=1"
    before, names = sha(db), sorted(p.name for p in folder.iterdir())
    assert names == ["sessions.db"]
    assert audit(db).session.tool_calls()
    assert audit("latest").session.tool_calls()
    GOOSE.detect(Path(db))
    GOOSE.resolve("latest").stat()
    assert cli.main(["list", "--agent", "goose"]) == 0
    out = capsys.readouterr().out
    assert "20260610_1" in out and "acme-api" in out
    assert sha(db) == before and sorted(p.name for p in folder.iterdir()) == names


def test_wal_db_with_a_lone_wal_file_is_refused(tmp_path):
    db = mk.demo(tmp_path / "sessions.db")
    _to_wal(db)
    (tmp_path / "sessions.db-wal").write_bytes(b"leftover")
    with pytest.raises(UnreadableSession):
        GOOSE.parse(gs.session_path(Path(db), "20260610_1"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sessions.db", "sessions.db-wal"]


def test_remote_target_is_never_opened(monkeypatch):
    monkeypatch.setattr(gs, "is_database", lambda p: pytest.fail("opened a remote path"))
    for remote in ("\\\\srv\\share\\sessions.db", "//srv/share/sessions.db"):
        with pytest.raises(FileNotFoundError):
            GOOSE.resolve(remote)
        assert not GOOSE.detect(Path(remote))


def test_symlinked_db_is_skipped(tmp_path, monkeypatch):
    folder = home(tmp_path, monkeypatch)
    real = mk.demo(tmp_path / "elsewhere.db")
    try:
        os.symlink(real, folder / "sessions.db")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink rights here")
    assert GOOSE.discover() == []
    assert not GOOSE.detect(folder / "sessions.db")


def test_session_path_stats_as_the_session(tmp_path):
    db = mk.demo(tmp_path / "sessions.db")
    path = GOOSE.resolve(db)
    assert 0 < path.stat().st_size < Path(db).stat().st_size
    assert path.stat().st_mtime == pytest.approx(mk.T0 + 8)
    assert audit(db).session.path.endswith("#20260610_1")


# -- SEC-207 / SEC-208, as tests/test_sec_207_208.py checks them for the other readers --

BOMB = "[" * 200_000 + "]" * 200_000
CLAIM = "I created `limiter.py` with the new limiter."
DOCUMENTED_EXITS = (0, 1, 2, 3)


def run_cli(target, agent, capsys, disk=False):
    argv = ["audit", str(target), "--agent", agent, "--no-color"]
    if not disk:
        argv.append("--no-disk-check")
    code = cli.main(argv)  # a crash raises here and fails the test
    out, err = capsys.readouterr()
    assert code in DOCUMENTED_EXITS
    assert "Traceback" not in out + err
    return code, out


def test_bomb_row_is_counted_not_a_crash(tmp_path, capsys):
    def build(b):
        b.say("Before.")
        b.raw("assistant", BOMB)
        b.say("After.")
    db = one_session(tmp_path, build)
    session = GOOSE.parse(GOOSE.resolve(db))
    assert session.unrecognised == 1
    assert [e.text for e in session.text_events()] == ["Before.", "After."]
    _, out = run_cli(db, "goose", capsys)
    assert "1 unrecognised record(s) skipped" in out
    run_cli(db, "auto", capsys)


def test_bomb_metadata_is_not_a_crash(tmp_path, capsys):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    row = b.user("Fix it.")
    b.conn.execute("UPDATE messages SET metadata_json = ? WHERE id = ?", (BOMB, row))
    b.say("Done.")
    db = b.close()
    session = GOOSE.parse(GOOSE.resolve(db))
    assert session.prompt_starts == [0]  # unreadable metadata hides nothing
    run_cli(db, "goose", capsys)


def _file_finding(result):
    found = [f for f in result.findings
             if f.claim.type in (ClaimType.FILE_CREATED, ClaimType.FILE_EDITED)]
    assert len(found) == 1, [f.claim for f in result.findings]
    return found[0]


WRITE_TOOLS = [
    ("write", lambda t: ("developer__write", {"path": t, "content": "x = 1\n"})),
    ("text_editor", lambda t: ("developer__text_editor",
                               {"command": "write", "path": t, "file_text": "x = 1\n"})),
]


@pytest.mark.parametrize("disk", (False, True), ids=("no-disk-check", "disk-check"))
@pytest.mark.parametrize("name,tool", WRITE_TOOLS, ids=[w[0] for w in WRITE_TOOLS])
def test_write_with_no_result_does_not_verify_created(tmp_path, name, tool, disk):
    db = one_session(tmp_path, lambda b: (
        b.tool(*tool((tmp_path / "limiter.py").as_posix()), respond=False), b.say(CLAIM)))
    write = GOOSE.parse(GOOSE.resolve(db)).tool_calls()[0]
    assert write.tool_action == WRITE and write.result_seen is False
    finding = _file_finding(cli.run_audit(db, check_disk=disk, agent="goose"))
    assert finding.verdict is Verdict.UNVERIFIED
    assert "no result was recorded" in finding.evidence


FAILURES = [
    ("error-result", {"error": "extension crashed"}),
    ("is-error", {"value": {"content": [{"type": "text", "text": "denied"}], "isError": True}}),
]


@pytest.mark.parametrize("disk", (False, True), ids=("no-disk-check", "disk-check"))
@pytest.mark.parametrize("failure,kw", FAILURES, ids=[f[0] for f in FAILURES])
@pytest.mark.parametrize("name,tool", WRITE_TOOLS, ids=[w[0] for w in WRITE_TOOLS])
def test_failed_write_does_not_verify_created(tmp_path, name, tool, failure, kw, disk):
    db = one_session(tmp_path, lambda b: (
        b.tool(*tool((tmp_path / "limiter.py").as_posix()), **kw), b.say(CLAIM)))
    write = GOOSE.parse(GOOSE.resolve(db)).tool_calls()[0]
    assert write.result_seen and write.is_error and write.status == "error"
    finding = _file_finding(cli.run_audit(db, check_disk=disk, agent="goose"))
    assert finding.verdict is Verdict.UNVERIFIED
    assert "did not succeed (error)" in finding.evidence


@pytest.mark.parametrize("name,tool", WRITE_TOOLS, ids=[w[0] for w in WRITE_TOOLS])
def test_no_result_then_successful_write_verifies(tmp_path, name, tool):
    target = (tmp_path / "limiter.py").as_posix()
    db = one_session(tmp_path, lambda b: (
        b.tool(*tool(target), respond=False), b.tool(*tool(target)), b.say(CLAIM)))
    finding = _file_finding(cli.run_audit(db, check_disk=False, agent="goose"))
    assert finding.verdict is Verdict.VERIFIED
