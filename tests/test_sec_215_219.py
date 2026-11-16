"""SEC-215..219: Goose (and opencode) reader Lows from the D087 security read.

Every value here is made up: no real run, no real path, no personal data.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
from pathlib import Path

from donediff import cli
from donediff.models import SHELL, ClaimType, Verdict
from donediff.readers import goose as gs
from donediff.readers import opencode as oc

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "make_goose_215", ROOT / "tests" / "fixtures" / "goose" / "make_goose.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)


def audit(target, **kw):
    return cli.run_audit(str(target), check_disk=False, agent="goose", **kw)


def pass_finding(target):
    found = [f for f in audit(target).findings if f.claim.type is ClaimType.TESTS_PASS]
    assert len(found) == 1
    return found[0]


# -- SEC-215: a whole-conversation rewrite hides a later edit and failure ---------

def _rewrite(b, keep: int) -> None:
    """What Goose's replace_conversation_inner does: delete every row of the
    session, insert the kept ones again (same content and time, new ids)."""
    rows = b.conn.execute("SELECT role, content_json, created_timestamp, metadata_json "
                          "FROM messages WHERE session_id = ? ORDER BY id", (b.sid,)).fetchall()
    b.conn.execute("DELETE FROM messages WHERE session_id = ?", (b.sid,))
    for role, content, created, metadata in rows[:keep]:
        b.conn.execute("INSERT INTO messages (session_id, role, content_json, created_timestamp, "
                       "metadata_json) VALUES (?, ?, ?, ?, ?)",
                       (b.sid, role, content, created, metadata))


def _pass_edit_fail(b) -> None:
    b.user("Please do the task.")
    b.shell("pytest -q", "12 passed", 0)  # rows 2 (request) and 3 (result)
    b.edit(mk.CWD + "/app.py", "x = 1", "x = 2")
    b.shell("pytest -q", "1 failed", 1)


def _assert_rewound(db) -> None:
    result = audit(db)
    run = [e for e in result.session.tool_calls() if e.tool_action == SHELL][0]
    assert run.exit_code is None and run.status == gs._REWOUND
    finding = [f for f in result.findings if f.claim.type is ClaimType.TESTS_PASS][0]
    assert finding.verdict is Verdict.UNVERIFIED and "exit 0" not in finding.evidence


def test_rewritten_only_session_hides_no_failure(tmp_path):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    _pass_edit_fail(b)
    _rewrite(b, keep=3)  # user + the passing run; the edit and the failure are gone
    b.say("All tests pass.")
    _assert_rewound(b.close())


def test_rewritten_session_after_another_session_hides_no_failure(tmp_path):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    _pass_edit_fail(b)
    b.session("20260610_8").user("another session")  # its row sits right below the rewrite
    b.sid = "20260610_7"
    _rewrite(b, keep=3)
    b.say("All tests pass.")
    _assert_rewound(b.close())


def test_sessions_one_after_another_are_not_a_rewrite(tmp_path):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_6")
    b.user("first session")
    b.say("Hello.")
    b.session("20260610_7")
    b.user("Please do the task.")
    b.shell("pytest -q", "12 passed", 0)
    b.say("All tests pass.")
    assert pass_finding(b.close() + "#20260610_7").verdict is Verdict.VERIFIED


# -- SEC-216: a relative or drive-relative APPDATA / home is never used as-is ------

def _env(monkeypatch, home, appdata=None, xdg=None):
    monkeypatch.delenv("GOOSE_PATH_ROOT", raising=False)
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    for var, value in (("APPDATA", appdata), ("XDG_DATA_HOME", xdg)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)


def test_relative_appdata_falls_back_to_the_absolute_home(tmp_path, monkeypatch):
    monkeypatch.setattr(gs.sys, "platform", "win32")
    for appdata in (r"rel\appdata", "rel/appdata", "C:relative"):
        _env(monkeypatch, tmp_path / "home", appdata=appdata)
        db = gs.database_path()
        assert db == (tmp_path / "home" / "AppData" / "Roaming" / "Block" / "goose" / "data"
                      / "sessions" / "sessions.db"), appdata
        assert db.is_absolute()


def test_relative_home_is_refused_not_resolved(tmp_path, monkeypatch):
    for platform, kw in (("win32", {"appdata": r"rel\appdata"}), ("linux", {"xdg": "rel/xdg"})):
        monkeypatch.setattr(gs.sys, "platform", platform)
        _env(monkeypatch, "relhome", **kw)
        assert gs.data_dir() is None and gs.database_path() is None, platform
        assert gs.GOOSE.discover() == []


# -- SEC-217: metadata_json capped, the whole read bounded ---------------------------

def test_oversize_metadata_is_counted_and_hides_nothing(tmp_path):
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    row = b.user("Please do the task.")
    big = '{"userVisible": false, "pad": "' + "x" * (70 * 1024) + '"}'
    b.conn.execute("UPDATE messages SET metadata_json = ? WHERE id = ?", (big, row))
    b.say("Done.")
    session = gs.GOOSE.parse(gs.GOOSE.resolve(b.close()))
    assert session.unrecognised == 1
    assert session.prompt_starts == [0]


def test_rows_past_the_size_bound_are_counted_and_prove_no_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "_MAX_TOTAL", 3000, raising=False)
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    b.user("Please do the task.")
    b.shell("pytest -q", "12 passed", 0)
    b.say("x" * 4000)  # past the bound: it and everything after it is not read
    b.shell("pytest -q", "1 failed", 1)
    b.say("All tests pass.")
    session = gs.GOOSE.parse(gs.GOOSE.resolve(b.close()))
    runs = [e for e in session.tool_calls() if e.tool_action == SHELL]
    assert len(runs) == 1 and runs[0].exit_code is None and runs[0].status == gs._REWOUND
    assert session.unrecognised == 4


def test_rows_past_the_row_bound_are_counted_and_prove_no_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "_MAX_ROWS", 3)
    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    b.user("Please do the task.")
    b.shell("pytest -q", "12 passed", 0)
    b.shell("pytest -q", "1 failed", 1)
    session = gs.GOOSE.parse(gs.GOOSE.resolve(b.close()))
    runs = [e for e in session.tool_calls() if e.tool_action == SHELL]
    assert len(runs) == 1 and runs[0].exit_code is None
    assert session.unrecognised == 2


def test_big_metadata_rows_stay_out_of_memory(tmp_path):
    import tracemalloc

    b = mk.Builder(tmp_path / "sessions.db").session("20260610_7")
    for _ in range(3):
        row = b.user("Please do the task.")
        b.conn.execute("UPDATE messages SET metadata_json = ? WHERE id = ?",
                       ("{" + " " * (8 * 1024 * 1024) + "}", row))
    target = gs.GOOSE.resolve(b.close())
    tracemalloc.start()
    try:
        session = gs.GOOSE.parse(target)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert session.unrecognised == 3
    assert peak < 4 * 1024 * 1024, peak


# -- SEC-218: a leftover -wal + -shm pair is read from a private copy ------------------

_spec_oc = importlib.util.spec_from_file_location(
    "make_opencode_218", ROOT / "tests" / "fixtures" / "opencode" / "make_opencode.py")
mk_oc = importlib.util.module_from_spec(_spec_oc)
_spec_oc.loader.exec_module(mk_oc)


def _leftover_pair(db, folder, update_sql) -> Path:
    """A WAL db copied with its live -wal and -shm (the SEC-218 repro); the
    update sits only in the -wal, so reading it proves the -wal was read."""
    folder.mkdir()
    writer = sqlite3.connect(db)
    assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute(update_sql)
    writer.commit()
    name = Path(db).name
    for suffix in ("", "-wal", "-shm"):
        shutil.copyfile(str(db) + suffix, folder / (name + suffix))
    writer.close()
    return folder / name


def _snapshot(folder) -> dict:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(folder.iterdir())}


def _assert_untouched_and_cleaned(reader, db, folder, before) -> None:
    assert _snapshot(folder) == before  # same names, bytes and mtimes
    assert reader._open_mode(db) == oc.COPY
    copies = [held[1] for held in oc._COPIES.values()]
    assert copies and all(not Path(c).is_relative_to(folder) for c in copies)
    oc.drop_copies()
    assert oc._COPIES == {} and not any(Path(c).exists() for c in copies)


def test_goose_leftover_wal_and_shm_are_never_written(tmp_path, monkeypatch, capsys):
    db = _leftover_pair(mk.demo(tmp_path / "sessions.db"), tmp_path / "left",
                        "UPDATE sessions SET working_dir = '/home/dev/walproof'")
    folder = db.parent
    before = _snapshot(folder)
    assert sorted(before) == ["sessions.db", "sessions.db-shm", "sessions.db-wal"]
    monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path / "root"))
    path = gs.GOOSE.resolve(str(db))
    assert gs.GOOSE.describe(path)[1] == "walproof"
    path.stat()
    assert gs.GOOSE.detect(db)
    assert audit(db).session.tool_calls()
    assert cli.main(["audit", str(db), "--agent", "goose", "--no-disk-check"]) in (0, 1)
    capsys.readouterr()
    _assert_untouched_and_cleaned(gs, db, folder, before)


def test_opencode_leftover_wal_and_shm_are_never_written(tmp_path, monkeypatch, capsys):
    db = _leftover_pair(mk_oc.demo(tmp_path / "opencode.db"), tmp_path / "left",
                        "UPDATE session_v2 SET directory = '/home/dev/walproof'")
    folder = db.parent
    before = _snapshot(folder)
    assert sorted(before) == ["opencode.db", "opencode.db-shm", "opencode.db-wal"]
    monkeypatch.setenv("OPENCODE_DB", str(db))
    path = oc.OPENCODE.resolve(str(db))
    assert oc.OPENCODE.describe(path)[1] == "walproof"
    path.stat()
    assert oc.OPENCODE.detect(db)
    assert cli.run_audit(str(db), check_disk=False, agent="opencode").session.tool_calls()
    assert cli.main(["list", "--agent", "opencode"]) == 0
    capsys.readouterr()
    _assert_untouched_and_cleaned(oc, db, folder, before)


def test_a_changed_source_gets_a_fresh_copy_and_the_old_one_is_deleted(tmp_path):
    db = _leftover_pair(mk.demo(tmp_path / "sessions.db"), tmp_path / "left",
                        "UPDATE sessions SET working_dir = '/home/dev/walproof'")
    first = oc.private_copy(db, "x")
    assert oc.private_copy(db, "x") == first  # unchanged source: one copy
    os.utime(str(db) + "-wal", ns=(1, 1))
    second = oc.private_copy(db, "x")
    assert second != first and not first.parent.exists() and second.is_file()
    oc.drop_copies()
    assert not second.parent.exists()
